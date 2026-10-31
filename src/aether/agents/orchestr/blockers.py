"""Blocker handling, escalation, and the user's authorization answers."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from aether.agents.orchestr.assignment import publish_assignment
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind
from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planner import (
    StepKind,
    blocker_idempotency_key,
    kind_of_idempotency_key,
)
from aether.agents.orchestr.routing import DEFAULT_ASSIGNEE, infer_kind, route_blocker
from aether.agents.orchestr.store import ACTOR, DecisionView, stable_id
from aether.agents.terminal import IGNORED_DECISION, TERMINAL_OBJECTIVE_STATES
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
_BLOCKABLE_NOW = (
    TaskStatus.PENDING,
    TaskStatus.READY,
    TaskStatus.WAITING_FOR_APPROVAL,
    TaskStatus.WAITING_FOR_VERIFICATION,
)
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
_MAX_TEXT = 2000
_RESOLVE_PREFIX = re.compile(r"^(?:Resolve [a-z_]+ blocker: )+")

# Escalation kinds that are not a BlockerKind.
TASK_FAILURE = "task_failure"
VERIFICATION = "verification"
PLAN = "plan"
REQUIREMENT = "requirement"
# Waits that clear on their own or by a person's reply: the user is told, the objective keeps
# running (a blocked objective would stop ORCHESTR releasing and completing its other tasks).
VERIFICATION_WAIT = "verification_wait"
APPROVAL_WAIT = "approval_wait"
RESOURCE_DEFERRAL = "resource_deferral"


async def escalate_objective(
    env: Env,
    objective: ObjectiveRecord,
    *,
    key: str,
    reason: str,
    kind: str,
    task_id: UUID | None = None,
    block: bool = True,
    decision: str | None = None,
) -> bool:
    """Stop work on an objective that cannot proceed unaided and tell the user once.

    ``key`` identifies the trigger (one blocker, one failed task, ...): each trigger is handled
    once. ``kind`` groups triggers: at most one escalation and one user update is sent per
    (objective, kind) per ``limits.escalation_window_hours``; later triggers in the window are
    recorded as ``escalation_aggregated`` against the open escalation. Finished objectives are
    never escalated. With ``block=False`` the user is told but the objective stays active.
    True when a new escalation (and user update) was issued.
    """
    current = await env.store.get_objective(objective.id) or objective
    if current.status in TERMINAL_OBJECTIVE_STATES:
        await env.store.record_decision(
            IGNORED_DECISION,
            f"objective is {current.status}; escalation not raised: {reason[:200]}",
            key=f"orchestr:ignored-terminal:escalation:{key}",
            evidence=[kind, current.status],
            objective_id=objective.id,
            task_id=task_id,
        )
        return False
    claimed = await env.store.record_decision(
        "escalation_requested",
        reason[:300],
        key=f"orchestr:escalation-trigger:{key}",
        evidence=[kind],
        objective_id=objective.id,
        task_id=task_id,
    )
    if not claimed:
        return False
    prefix = f"orchestr:escalated:{objective.id}:{kind}:"
    previous = await env.store.keyed_decisions(prefix)
    now = env.now()
    open_escalation = _open_escalation(previous, now, env.limits.escalation_window_hours)
    if open_escalation is None:
        open_key = f"{prefix}{len(previous)}"
        if await env.store.record_decision(
            "escalated",
            reason[:300],
            key=open_key,
            evidence=[kind, now.isoformat(), key],
            objective_id=objective.id,
            task_id=task_id,
        ):
            await _escalate_now(
                env, current, objective.id, reason, kind, len(previous), task_id, block, decision
            )
            return True
    else:
        open_key = open_escalation.key or prefix
    await env.store.record_decision(
        "escalation_aggregated",
        f"aggregated into the open {kind} escalation: {reason[:250]}",
        key=f"orchestr:aggregated:{key}",
        evidence=[kind, open_key],
        objective_id=objective.id,
        task_id=task_id,
    )
    return False


def _open_escalation(
    previous: list[DecisionView], now: datetime, window_hours: float
) -> DecisionView | None:
    window = timedelta(hours=window_hours)
    for view in reversed(previous):
        try:
            raised = datetime.fromisoformat(view.evidence[1])
        except (IndexError, ValueError):
            continue
        if raised.tzinfo is None:
            raised = raised.replace(tzinfo=UTC)
        if now - raised < window:
            return view
    return None


async def _escalate_now(
    env: Env,
    current: ObjectiveRecord,
    objective_id: UUID,
    reason: str,
    kind: str,
    number: int,
    task_id: UUID | None,
    block: bool = True,
    decision: str | None = None,
) -> None:
    if block and current.status == "active":
        await env.store.objectives.set_status(
            current.id, "blocked", actor=ACTOR, reason=reason[:300]
        )
        current = await env.store.get_objective(objective_id) or current
    headline = (
        f"Work on '{current.title}' is blocked and needs your input."
        if block
        else f"Work on '{current.title}' is taking longer than expected."
    )
    await env.send_user_update(
        UpdateEvent(UpdateKind.ESCALATION),
        key=f"escalation:{objective_id}:{kind}:{number}",
        what_happened=headline,
        objective=current,
        task_id=task_id,
        blockers=[reason],
        decisions_required=[
            decision or "Decide whether to retry, change the approach, or cancel the objective"
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
    # A resolution task is a replan, bounded per objective; past a bound the user decides.
    limit_reason = (
        await _resolution_limit(env, objective, task, payload) if route.creates_task else None
    )
    to_user = route.escalate or limit_reason is not None

    if to_user:
        await _block_task(env, task)
        reason = (
            f"{payload.kind.value} blocker persisted after {attempts} attempts: {description[:200]}"
            if route.escalate
            else f"{payload.kind.value} blocker needs a decision ({limit_reason}): "
            f"{description[:200]}"
        )
        await escalate_objective(
            env,
            objective,
            key=f"blocker:{payload.blocker_id}",
            reason=reason,
            kind=payload.kind.value,
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
        f"{route.assignee.value if route.assignee and not to_user else 'user'}: "
        f"{description[:200]}",
        key=marker,
        evidence=[payload.kind.value, str(attempts), str(payload.route_to.value)],
        objective_id=objective.id,
        task_id=task.id,
    )


def _marker(blocker_id: UUID) -> str:
    return f"orchestr:blocker:{blocker_id}"


def _replan_marker(objective_id: UUID, blocker_id: UUID) -> str:
    return f"orchestr:replan:{objective_id}:blocker:{blocker_id}"


async def _resolution_limit(
    env: Env, objective: ObjectiveRecord, task: TaskRecord, payload: BlockerPayload
) -> str | None:
    """Why no resolution task may be planned for this blocker, or ``None`` when one may.

    A resolution task is never itself resolved by another task (that nesting re-planned
    without bound); it also counts as a replan and as a task of the objective.
    """
    key = blocker_idempotency_key(payload.blocker_id, "resolve")
    if await env.store.task_by_key(key) is not None:
        return None  # redelivered after the task was created: reuse it
    limits = env.limits
    if limits.max_replans_per_objective <= 0:
        return "automatic replanning is disabled"
    if kind_of_idempotency_key(task.idempotency_key) is StepKind.BLOCKER:
        return "the blocker resolution task is itself blocked"
    replans = await env.store.decisions(objective_id=objective.id, decision="replan")
    if len(replans) >= limits.max_replans_per_objective:
        return f"automatic replanning limit of {limits.max_replans_per_objective} reached"
    if await env.store.count_tasks(objective.id) >= limits.max_tasks_per_objective:
        return f"objective task limit of {limits.max_tasks_per_objective} reached"
    return None


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
        await env.store.record_decision(
            "replan",
            f"{payload.kind.value} blocker resolution task planned for '{task.title[:120]}'",
            key=_replan_marker(objective.id, payload.blocker_id),
            objective_id=objective.id,
            task_id=task.id,
        )
        resolution_id = await env.store.tasks.create(
            objective_id=objective.id,
            title=f"Resolve {payload.kind.value} blocker: {_base_title(task.title)}"[:200],
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


def _base_title(title: str) -> str:
    """The title without any earlier ``Resolve <kind> blocker:`` prefixes (titles never nest)."""
    return _RESOLVE_PREFIX.sub("", title) or title


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
                kind=TASK_FAILURE,
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
