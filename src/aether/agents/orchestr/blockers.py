"""Blocker handling, escalation, and the user's authorization answers."""

from uuid import UUID

from aether.agents.orchestr.assignment import publish_assignment
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind
from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planner import blocker_idempotency_key
from aether.agents.orchestr.routing import DEFAULT_ASSIGNEE, infer_kind, route_blocker
from aether.agents.orchestr.store import ACTOR, stable_id
from aether.communication.contracts import (
    AuthorizationRequestPayload,
    BlockerKind,
    BlockerPayload,
    TaskOutcome,
    TaskResultPayload,
    UserDecision,
    UserResponsePayload,
)
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import ObjectiveRecord, TaskRecord, TaskStatus
from aether.storage.repositories import InvalidAuthorizationResponse, InvalidTaskTransition

# States ORCHESTR may move to BLOCKED itself. Running work is left to the worker pool, which
# records BLOCKED when the agent returns; moving it here would race that transition.
_BLOCKABLE_NOW = (TaskStatus.PENDING, TaskStatus.READY, TaskStatus.WAITING_FOR_APPROVAL)
_CANCELLABLE = (
    TaskStatus.PENDING,
    TaskStatus.READY,
    TaskStatus.SCHEDULED,
    TaskStatus.BLOCKED,
    TaskStatus.PAUSED,
    TaskStatus.WAITING_FOR_APPROVAL,
    TaskStatus.RETRYING,
    TaskStatus.FAILED,
)
_MAX_TEXT = 2000


async def escalate_objective(
    env: Env,
    objective: ObjectiveRecord,
    *,
    key: str,
    reason: str,
    task_id: UUID | None = None,
) -> None:
    """Stop work on an objective that cannot proceed unaided and tell the user once."""
    current = await env.store.get_objective(objective.id) or objective
    if current.status == "active":
        await env.store.objectives.set_status(
            current.id, "blocked", actor=ACTOR, reason=reason[:300]
        )
        current = await env.store.get_objective(objective.id) or current
    await env.store.record_decision(
        "escalated",
        reason[:300],
        key=f"orchestr:escalated:{key}",
        objective_id=objective.id,
        task_id=task_id,
    )
    await env.send_user_update(
        UpdateEvent(UpdateKind.ESCALATION),
        key=f"escalation:{key}",
        what_happened=f"Work on '{current.title}' is blocked and needs your input.",
        objective=current,
        task_id=task_id,
        blockers=[reason],
        decisions_required=[
            "Decide whether to retry, change the approach, or cancel the objective"
        ],
    )


async def _prior_blockers(env: Env, task_id: UUID) -> int:
    return len(await env.store.decisions(task_id=task_id, decision="blocker"))


async def handle_blocker(env: Env, payload: BlockerPayload, message: AgentMessage | None) -> None:
    task = await env.store.get_task(payload.task_id)
    if task is None:
        await env.store.record_decision(
            "blocker_ignored", "blocker references an unknown task", key=_marker(payload.blocker_id)
        )
        return
    marker = _marker(payload.blocker_id)
    if await env.store.get_decision(marker) is not None:
        return
    objective = await env.store.get_objective(task.objective_id)
    if objective is None:
        return
    attempts = max(payload.attempts, await _prior_blockers(env, task.id))
    route = route_blocker(
        payload.kind,
        attempts=attempts,
        max_attempts=env.limits.max_blocker_attempts,
        route_to=payload.route_to,
    )
    description = payload.description[:_MAX_TEXT]

    if route.escalate:
        await _block_task(env, task)
        await escalate_objective(
            env,
            objective,
            key=f"blocker:{payload.blocker_id}",
            reason=f"{payload.kind.value} blocker persisted after {attempts} attempts: "
            f"{description[:200]}",
            task_id=task.id,
        )
    elif payload.kind is BlockerKind.AUTHORIZATION:
        await _ask_for_authorization(env, objective, task, description, payload.blocker_id)
    elif route.creates_task and route.assignee is not None:
        await _create_resolution_task(env, objective, task, route.assignee, payload, description)
    else:
        # Resource blocks wait for the scheduler's own gating; the task goes back in the queue.
        await _requeue(env, task)
    await env.store.record_decision(
        "blocker",
        f"{payload.kind.value} blocker routed to "
        f"{route.assignee.value if route.assignee else 'user'}: {description[:200]}",
        key=marker,
        evidence=[payload.kind.value, str(attempts), str(payload.route_to.value)],
        objective_id=objective.id,
        task_id=task.id,
    )


def _marker(blocker_id: UUID) -> str:
    return f"orchestr:blocker:{blocker_id}"


async def _block_task(env: Env, task: TaskRecord) -> None:
    fresh = await env.store.get_task(task.id)
    if fresh is not None and TaskStatus(fresh.state) in _BLOCKABLE_NOW:
        await env.store.tasks.transition(
            task.id, TaskStatus.BLOCKED, actor=ACTOR, details={"reason": "blocker reported"}
        )


async def _requeue(env: Env, task: TaskRecord) -> None:
    fresh = await env.store.get_task(task.id)
    if fresh is not None and TaskStatus(fresh.state) is TaskStatus.BLOCKED:
        await env.store.tasks.transition(
            task.id,
            TaskStatus.PENDING,
            actor=ACTOR,
            details={"reason": "resource blocker requeued"},
        )


async def _create_resolution_task(
    env: Env,
    objective: ObjectiveRecord,
    task: TaskRecord,
    assignee: AgentId,
    payload: BlockerPayload,
    description: str,
) -> None:
    key = blocker_idempotency_key(payload.blocker_id, "resolve")
    existing = await env.store.task_by_key(key)
    if existing is None:
        resolution_id = await env.store.tasks.create(
            objective_id=objective.id,
            title=f"Resolve {payload.kind.value} blocker: {task.title}"[:200],
            description=description,
            assigned_agent=assignee.value,
            priority=min(100, task.priority + 10),
            estimated_ram_mb=128,
            estimated_disk_mb=10,
            max_attempts=env.limits.max_task_attempts,
            parent_task_id=task.id,
            deadline=objective.deadline,
            estimated_cpu_percent=10.0,
            idempotency_key=key,
            actor=ACTOR,
        )
    else:
        resolution_id = existing.id
    await env.store.dependencies.add(task.id, resolution_id, actor=ACTOR)
    await _block_task(env, task)


async def _ask_for_authorization(
    env: Env,
    objective: ObjectiveRecord,
    task: TaskRecord,
    description: str,
    blocker_id: UUID,
) -> None:
    pending = await env.store.pending_authorization_for(task.id)
    request_id = (
        pending.id
        if pending is not None
        else await env.store.authorizations.create(
            question=description[:500],
            actor=ACTOR,
            objective_id=objective.id,
            task_id=task.id,
        )
    )
    await _block_task(env, task)
    await env.send_user_update(
        UpdateEvent(UpdateKind.AUTHORIZATION_REQUIRED),
        key=f"authorization:{request_id}",
        what_happened=f"Task '{task.title}' needs your authorization to continue.",
        objective=objective,
        task_id=task.id,
        blockers=[description[:300]],
        decisions_required=["Approve or deny this request"],
        authorization_request_id=request_id,
    )


async def handle_task_result(env: Env, payload: TaskResultPayload, message: AgentMessage) -> None:
    """Act on a reported outcome. Completion and retries are the worker pool's to record."""
    task = await env.store.get_task(payload.task_id)
    if task is None:
        return
    if payload.status is TaskOutcome.BLOCKED:
        kind = infer_kind(task.assigned_agent)
        blocker = BlockerPayload(
            blocker_id=_result_blocker_id(message),
            task_id=task.id,
            objective_id=task.objective_id,
            kind=kind,
            description=payload.error or payload.summary or "task reported blocked",
            route_to=DEFAULT_ASSIGNEE[kind],
        )
        await handle_blocker(env, blocker, message)
    elif payload.status is TaskOutcome.FAILED and not payload.retryable:
        objective = await env.store.get_objective(task.objective_id)
        if objective is not None and objective.status == "active":
            await escalate_objective(
                env,
                objective,
                key=f"task:{task.id}",
                reason=f"task '{task.title}' failed and cannot be retried: "
                f"{(payload.error or '')[:200]}",
                task_id=task.id,
            )


def _result_blocker_id(message: AgentMessage) -> UUID:
    return stable_id("result-blocker", message.message_id)


async def forward_authorization_request(
    env: Env, payload: AuthorizationRequestPayload, message: AgentMessage
) -> None:
    """A WARDEN authorization request addressed to ORCHESTR: pause the task and ask the user."""
    task = await env.store.get_task(payload.task_id) if payload.task_id else None
    objective_id = payload.objective_id or (task.objective_id if task else None)
    objective = await env.store.get_objective(objective_id) if objective_id else None
    if task is not None:
        await _block_task(env, task)
    await env.send_user_update(
        UpdateEvent(UpdateKind.AUTHORIZATION_REQUIRED),
        key=f"authorization:{payload.authorization_request_id}",
        what_happened=payload.question,
        objective=objective,
        task_id=task.id if task else None,
        blockers=[payload.operation_summary],
        decisions_required=["Approve or deny this request"],
        authorization_request_id=payload.authorization_request_id,
    )


async def handle_user_response(
    env: Env, payload: UserResponsePayload, message: AgentMessage
) -> None:
    """Apply an explicit answer. Only ``approved`` unblocks; silence or free text never does."""
    request_id = payload.authorization_request_id
    if request_id is None or payload.decision is UserDecision.NONE:
        await env.store.record_decision(
            "user_response_noted",
            "user reply without an authorization decision; no state changed",
            key=f"orchestr:user-response:{message.message_id}",
            objective_id=payload.objective_id,
            task_id=payload.task_id,
        )
        return
    request = await env.store.get_authorization(request_id)
    if request is None:
        await env.store.record_decision(
            "user_response_ignored",
            "authorization request is unknown",
            key=f"orchestr:user-response:{message.message_id}",
        )
        return
    approved = payload.decision is UserDecision.APPROVED
    try:
        status = await env.store.authorizations.respond(
            request_id,
            approved=approved,
            actor=message.source_agent.value,
            response=payload.text or None,
            now=env.now(),
            correlation_id=message.correlation_id,
        )
    except InvalidAuthorizationResponse:
        return
    if status.value != ("approved" if approved else "denied"):
        return
    task = await env.store.get_task(request.task_id) if request.task_id else None
    if task is None:
        return
    state = TaskStatus(task.state)
    if approved:
        await _resume_after_approval(env, task, state)
    else:
        await _cancel_after_denial(env, task, state)


async def _resume_after_approval(env: Env, task: TaskRecord, state: TaskStatus) -> None:
    try:
        if state is TaskStatus.WAITING_FOR_APPROVAL:
            await env.store.tasks.transition(
                task.id, TaskStatus.READY, actor=ACTOR, details={"reason": "user approved"}
            )
            objective = await env.store.get_objective(task.objective_id)
            refreshed = await env.store.get_task(task.id)
            if objective is not None and refreshed is not None:
                await publish_assignment(env, refreshed, objective)
        elif state is TaskStatus.BLOCKED:
            await env.store.tasks.transition(
                task.id, TaskStatus.PENDING, actor=ACTOR, details={"reason": "user approved"}
            )
    except InvalidTaskTransition:
        return


async def _cancel_after_denial(env: Env, task: TaskRecord, state: TaskStatus) -> None:
    if state not in _CANCELLABLE:
        return
    await env.store.tasks.transition(
        task.id, TaskStatus.CANCELLED, actor=ACTOR, details={"reason": "user denied"}
    )
    objective = await env.store.get_objective(task.objective_id)
    if objective is not None and objective.status == "active":
        await env.store.objectives.set_status(
            objective.id, "blocked", actor=ACTOR, reason="authorization denied by the user"
        )
