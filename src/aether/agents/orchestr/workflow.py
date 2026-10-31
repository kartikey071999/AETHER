"""Message dispatch for ORCHESTR: one validated handler per accepted message type."""

from collections.abc import Awaitable, Callable

from aether.agents.orchestr.blockers import (
    forward_authorization_request,
    handle_blocker,
    handle_task_result,
    handle_user_response,
)
from aether.agents.orchestr.completion import handle_test_report
from aether.agents.orchestr.env import Env
from aether.agents.orchestr.intake import ObjectiveRejected, create_objective
from aether.agents.orchestr.linking import handle_consolidated, handle_evaluated
from aether.agents.orchestr.planner import plan_idempotency_key
from aether.agents.orchestr.planning import plan_objective
from aether.agents.orchestr.priority import format_boost_reason, format_set_reason
from aether.agents.orchestr.store import ACTOR
from aether.agents.terminal import IGNORED_DECISION, terminal_objective
from aether.communication.contracts import (
    ContractViolation,
    HealthState,
    ObjectiveCreatePayload,
    ObjectiveState,
    ObjectiveUpdatePayload,
    Payload,
    ResourceRequestPayload,
    TaskCreatePayload,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.scheduler import SchedulerResources, SchedulingCandidate
from aether.storage.models import ObjectiveRecord, TaskRecord, TaskStatus
from aether.storage.repositories import InvalidTaskTransition

_MIB = 1024 * 1024
_PRIVILEGED = frozenset({AgentId.MUSE, AgentId.ORCHESTR})
_NOT_ASSIGNABLE = frozenset({AgentId.ORCHESTR, AgentId.WARDEN, AgentId.MUSE})
_TASK_CREATE_STATUSES = ("active", "paused", "blocked")
_CANCELLABLE = (
    TaskStatus.PENDING,
    TaskStatus.READY,
    TaskStatus.SCHEDULED,
    TaskStatus.BLOCKED,
    TaskStatus.PAUSED,
    TaskStatus.WAITING_FOR_APPROVAL,
    TaskStatus.WAITING_FOR_VERIFICATION,
    TaskStatus.RETRYING,
    TaskStatus.FAILED,
)
_UNSUPPORTED_FIELDS = (
    "description",
    "completion_criteria",
    "stopping_conditions",
    "deadline",
    "review_at",
)

Handler = Callable[[Env, AgentMessage, Payload], Awaitable[None]]


async def dispatch(env: Env, message: AgentMessage) -> None:
    """Validate the message against its contract, record violations, then handle it."""
    try:
        payload = validate_message(message)
    except ContractViolation as error:
        await env.store.record_decision(
            "message_rejected",
            str(error)[:300],
            key=f"orchestr:invalid:{message.message_id}",
            evidence=[message.source_agent.value, message.message_type.value],
            correlation_id=message.correlation_id,
        )
        raise
    handler = _HANDLERS.get(message.message_type, _observe)
    if message.message_type in _TERMINAL_GUARDED and await _ignore_terminal(env, message):
        return
    await handler(env, message, payload)


# Work/progress messages that must not act on a cancelled, completed or failed objective.
_TERMINAL_GUARDED = frozenset(
    {
        MessageType.BLOCKER,
        MessageType.TASK_RESULT,
        MessageType.TEST_REPORT,
        MessageType.PROGRESS_UPDATE,
        MessageType.RESEARCH_CONSOLIDATED,
        MessageType.REQUIREMENT_EVALUATED,
        MessageType.AUTHORIZATION_REQUEST,
        MessageType.IMPLEMENTATION_PLAN,
        MessageType.EXECUTION_RESULT,
        MessageType.RESOURCE_REQUEST,
    }
)


async def _ignore_terminal(env: Env, message: AgentMessage) -> bool:
    """Acknowledge a message for a finished objective: no escalation, replan or user update."""
    objective = await terminal_objective(env.store.sessions, message)
    if objective is None:
        return False
    await env.store.record_decision(
        IGNORED_DECISION,
        f"objective is {objective.status}; {message.message_type.value} from "
        f"{message.source_agent.value} acknowledged without action",
        key=f"orchestr:ignored-terminal:{message.message_id}",
        evidence=[message.message_type.value, message.source_agent.value, objective.status],
        objective_id=objective.id,
        task_id=message.task_id,
        correlation_id=message.correlation_id,
    )
    return True


# --- objectives ----------------------------------------------------------------------------


async def _objective_create(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, ObjectiveCreatePayload)
    try:
        await create_objective(env, message, payload)
    except ObjectiveRejected:
        return  # already recorded; retrying the same message cannot succeed


async def _objective_update(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, ObjectiveUpdatePayload)
    marker = f"orchestr:update:{message.message_id}"
    if await env.store.get_decision(marker) is not None:
        return
    objective = await env.store.get_objective(payload.objective_id)
    source = message.source_agent
    if objective is None:
        await env.store.record_decision(
            "objective_update_ignored", "objective does not exist", key=marker
        )
        return

    notes: list[str] = []
    if payload.status is not None:
        notes.append(await _apply_status(env, objective, payload, source))
    if payload.priority is not None:
        notes.append(await _apply_priority(env, objective, payload, source))
    unsupported = [name for name in _UNSUPPORTED_FIELDS if getattr(payload, name) is not None]
    if unsupported:
        notes.append(f"unsupported fields not applied: {', '.join(unsupported)}")
    await env.store.record_decision(
        "objective_update",
        "; ".join(notes) or "no change requested",
        key=marker,
        evidence=[source.value, payload.reason[:200]],
        objective_id=objective.id,
        correlation_id=message.correlation_id,
    )


async def _apply_priority(
    env: Env, objective: ObjectiveRecord, payload: ObjectiveUpdatePayload, source: AgentId
) -> str:
    assert payload.priority is not None
    if source in _PRIVILEGED:
        new = payload.priority
        reason = format_set_reason(new, f"{source.value}: {payload.reason}"[:200])
        note = f"priority set to {new}"
    else:
        # Agents can only nudge an objective up, by a bounded, decaying amount.
        points = min(
            max(0, payload.priority - objective.priority), env.limits.max_agent_boost_points
        )
        if points == 0:
            return "priority boost ignored: no increase requested"
        new = min(100, objective.priority + points)
        reason = format_boost_reason(points, f"{source.value}: {payload.reason}"[:200])
        note = f"priority boosted by {points} (bounded)"
    if new != objective.priority or source in _PRIVILEGED:
        await env.store.objectives.update_priority(
            objective.id, new, actor=source.value, reason=reason
        )
    return note


async def _apply_status(
    env: Env, objective: ObjectiveRecord, payload: ObjectiveUpdatePayload, source: AgentId
) -> str:
    assert payload.status is not None
    wanted = payload.status
    if source not in _PRIVILEGED:
        return f"status change to {wanted.value} refused: {source.value} may not change status"
    if wanted.value == objective.status:
        return f"status already {wanted.value}"
    if objective.status in ("completed", "cancelled"):
        return f"status change refused: objective is already {objective.status}"
    if wanted is ObjectiveState.COMPLETED:
        return "completion refused: objectives complete only through verified acceptance criteria"

    if (
        wanted is ObjectiveState.ACTIVE
        and await env.store.count_objectives(["active"]) >= env.limits.max_active_objectives
    ):
        return "resume refused: active objective limit reached"
    await env.store.objectives.set_status(
        objective.id, wanted.value, actor=source.value, reason=payload.reason
    )
    tasks = await env.store.tasks_of(objective.id)
    if wanted is ObjectiveState.CANCELLED:
        for task in tasks:
            state = TaskStatus(task.state)
            if state in _CANCELLABLE:
                if not await _move(env, task, TaskStatus.CANCELLED, "objective cancelled"):
                    # A worker moved the task first (e.g. leased it): stop it cooperatively.
                    await _request_cancel(env, task, payload.reason)
            elif state is TaskStatus.RUNNING:
                await _request_cancel(env, task, payload.reason)
    elif wanted in (ObjectiveState.PAUSED, ObjectiveState.DEFERRED):
        for task in tasks:
            if TaskStatus(task.state) is TaskStatus.READY:
                await _move(env, task, TaskStatus.PAUSED, f"objective {wanted.value}")
    elif wanted is ObjectiveState.ACTIVE:
        for task in tasks:
            if TaskStatus(task.state) is TaskStatus.PAUSED:
                await _move(env, task, TaskStatus.READY, "objective resumed")
        fresh = await env.store.get_objective(objective.id)
        if fresh is not None:
            await plan_objective(env, fresh)
    return f"status changed to {wanted.value}"


async def _request_cancel(env: Env, task: TaskRecord, reason: str) -> None:
    """Running work cannot be moved directly: ask the runtime to stop it cooperatively."""
    detail = f"objective cancelled: {reason}"[:300]
    if env.control is None:
        await env.store.record_decision(
            "task_cancel_unavailable",
            "no task control service; running task left to finish",
            key=f"orchestr:cancel-unavailable:{task.id}",
            evidence=[detail],
            objective_id=task.objective_id,
            task_id=task.id,
        )
        return
    try:
        result = await env.control.request_cancel(task.id, reason=detail)
    except LookupError:
        return
    await env.store.record_decision(
        "task_cancel_requested",
        detail,
        key=f"orchestr:cancel-requested:{task.id}",
        evidence=[result.outcome, result.state.value],
        objective_id=task.objective_id,
        task_id=task.id,
    )


async def _move(env: Env, task: TaskRecord, status: TaskStatus, reason: str) -> bool:
    try:
        await env.store.tasks.transition(task.id, status, actor=ACTOR, details={"reason": reason})
    except InvalidTaskTransition:
        return False
    return True


# --- tasks ---------------------------------------------------------------------------------


async def _reject_task(env: Env, message: AgentMessage, reason: str) -> None:
    await env.store.record_decision(
        "task_rejected",
        reason,
        key=f"orchestr:task-rejected:{message.message_id}",
        evidence=[message.source_agent.value],
        objective_id=message.objective_id,
    )


async def _task_create(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, TaskCreatePayload)
    key = f"task.create:{message.message_id}"
    if await env.store.task_by_key(key) is not None:
        return
    objective = await env.store.get_objective(payload.objective_id)
    agent = payload.suggested_agent
    if objective is None or objective.status not in _TASK_CREATE_STATUSES:
        return await _reject_task(env, message, "objective is unknown or no longer accepts tasks")
    if agent is None or agent in _NOT_ASSIGNABLE:
        return await _reject_task(env, message, "a worker agent must be named for the task")
    limits = env.limits
    if await env.store.count_tasks(objective.id) >= limits.max_tasks_per_objective:
        return await _reject_task(env, message, "objective task limit reached")
    if payload.parent_task_id is not None:
        parent = await env.store.get_task(payload.parent_task_id)
        if parent is None or parent.objective_id != objective.id:
            return await _reject_task(env, message, "parent task is not part of this objective")
        if await env.store.child_depth(parent.id) + 1 > limits.max_subtask_depth:
            return await _reject_task(env, message, "subtask depth limit reached")

    depends_on = set(payload.depends_on)
    if depends_on:
        states = await env.store.states_of(depends_on)
        known = {task.id for task in await env.store.tasks_of(objective.id)}
        if set(states) != depends_on or not depends_on <= known:
            return await _reject_task(env, message, "dependencies must be tasks of this objective")
    if agent is AgentId.FORGE:
        review = await env.store.task_by_key(plan_idempotency_key(objective.id, "security_review"))
        if review is not None:
            depends_on.add(review.id)

    task_id = await env.store.tasks.create(
        objective_id=objective.id,
        title=payload.title,
        description=payload.description,
        assigned_agent=agent.value,
        priority=payload.priority,
        estimated_ram_mb=payload.resource_estimate.ram_mb,
        estimated_disk_mb=payload.resource_estimate.disk_mb,
        max_attempts=limits.max_task_attempts,
        parent_task_id=payload.parent_task_id,
        requirement_id=payload.requirement_id,
        deadline=payload.deadline or objective.deadline,
        interruptible=payload.interruptible,
        estimated_cpu_percent=payload.resource_estimate.cpu_percent,
        idempotency_key=key,
        actor=ACTOR,
        correlation_id=message.correlation_id,
    )
    for dependency in depends_on:
        await env.store.dependencies.add(task_id, dependency, actor=ACTOR)
    await env.store.record_decision(
        "task_created",
        f"task accepted from {message.source_agent.value} for {agent.value}",
        key=f"orchestr:task-created:{message.message_id}",
        objective_id=objective.id,
        task_id=task_id,
    )


async def _task_result(env: Env, message: AgentMessage, payload: Payload) -> None:
    await handle_task_result(env, payload, message)  # type: ignore[arg-type]


async def _blocker(env: Env, message: AgentMessage, payload: Payload) -> None:
    await handle_blocker(env, payload, message)  # type: ignore[arg-type]


async def _authorization_request(env: Env, message: AgentMessage, payload: Payload) -> None:
    await forward_authorization_request(env, payload, message)  # type: ignore[arg-type]


async def _user_response(env: Env, message: AgentMessage, payload: Payload) -> None:
    await handle_user_response(env, payload, message)  # type: ignore[arg-type]


async def _test_report(env: Env, message: AgentMessage, payload: Payload) -> None:
    await handle_test_report(env, payload, message)  # type: ignore[arg-type]


# --- resources and informational messages -----------------------------------------------------


async def _resource_request(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, ResourceRequestPayload)
    key = f"orchestr:resource:{message.message_id}"
    if await env.store.get_decision(key) is not None:
        return
    task = await env.store.get_task(payload.task_id) if payload.task_id else None
    try:
        host = env.monitor.snapshot(env.disk_path)
    except Exception:
        env.logger.exception("resource snapshot failed while evaluating a resource request")
        await env.store.record_decision(
            "resource_request_deferred", "host resources could not be read", key=key
        )
        return
    occupying = await env.store.occupying_tasks()
    candidate = SchedulingCandidate(
        task_id=payload.task_id or message.message_id,
        priority=task.priority if task else message.priority,
        waiting_since=env.now(),
        estimated_ram_bytes=payload.resources.ram_mb * _MIB,
        estimated_disk_bytes=(payload.resources.disk_mb + payload.download_mb) * _MIB,
        estimated_cpu_percent=payload.resources.cpu_percent,
    )
    decision = env.scheduler.choose_next(
        [candidate],
        SchedulerResources(host=host, running_tasks=len(occupying), running_builds=0),
        now=env.now(),
    )
    granted = decision.selected_task_id is not None
    reason = decision.deferred[0].reason if decision.deferred else decision.reason
    await env.store.record_decision(
        "resource_request_approved" if granted else "resource_request_deferred",
        "fits current limits" if granted else reason,
        key=key,
        evidence=[message.source_agent.value, payload.reason[:200]],
        objective_id=message.objective_id,
        task_id=payload.task_id,
    )


async def _observe(env: Env, message: AgentMessage, payload: Payload) -> None:
    """Informational messages: state is driven by tasks, so ORCHESTR only keeps an audit trail."""
    await env.store.record_decision(
        "message_observed",
        f"{message.message_type.value} from {message.source_agent.value}",
        key=f"orchestr:observed:{message.message_id}",
        objective_id=message.objective_id,
        task_id=message.task_id,
    )


async def _health(env: Env, message: AgentMessage, payload: Payload) -> None:
    status = getattr(payload, "status", HealthState.HEALTHY)
    if status is not HealthState.HEALTHY:
        await _observe(env, message, payload)


async def _quiet(env: Env, message: AgentMessage, payload: Payload) -> None:
    return None


_HANDLERS: dict[MessageType, Handler] = {
    MessageType.OBJECTIVE_CREATE: _objective_create,
    MessageType.OBJECTIVE_UPDATE: _objective_update,
    MessageType.TASK_CREATE: _task_create,
    MessageType.TASK_RESULT: _task_result,
    MessageType.BLOCKER: _blocker,
    MessageType.AUTHORIZATION_REQUEST: _authorization_request,
    MessageType.USER_RESPONSE: _user_response,
    MessageType.TEST_REPORT: _test_report,
    MessageType.RESOURCE_REQUEST: _resource_request,
    MessageType.HEALTH_EVENT: _health,
    MessageType.TASK_TRANSITION: _quiet,
    MessageType.PROGRESS_UPDATE: _quiet,
    MessageType.COMMUNICATION_DELIVERED: _quiet,
    MessageType.IMPLEMENTATION_PLAN: _observe,
    MessageType.RESEARCH_CONSOLIDATED: handle_consolidated,
    MessageType.REQUIREMENT_EVALUATED: handle_evaluated,
    MessageType.CAPABILITY_REGISTER: _observe,
    MessageType.EXECUTION_RESULT: _observe,
}

__all__ = ["dispatch"]
