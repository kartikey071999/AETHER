"""The scheduling tick: promote, re-prioritize, reconcile, release, detect completion."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from aether.agents.orchestr.assignment import release_task
from aether.agents.orchestr.blockers import (
    APPROVAL_WAIT,
    REQUIREMENT,
    RESOURCE_DEFERRAL,
    TASK_FAILURE,
    VERIFICATION_WAIT,
    escalate_objective,
)
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind
from aether.agents.orchestr.completion import evaluate_completion
from aether.agents.orchestr.env import Env, as_utc
from aether.agents.orchestr.intake import LIMIT_DEFERRAL_REASON
from aether.agents.orchestr.linking import held_for_requirement
from aether.agents.orchestr.planning import plan_objective
from aether.agents.orchestr.pressure import is_build, manage_pressure
from aether.agents.orchestr.priorities import refresh_all, task_rank
from aether.agents.orchestr.store import ACTOR
from aether.runtime.scheduler import DeferredTask, SchedulerResources, SchedulingCandidate
from aether.storage.models import ObjectiveRecord, TaskRecord, TaskStatus
from aether.storage.repositories import InvalidTaskTransition

_MIB = 1024 * 1024
_DEFAULT_CPU = 25.0
_FAILED_DEPENDENCY = (TaskStatus.FAILED, TaskStatus.CANCELLED)


@dataclass(slots=True)
class TickReport:
    promoted: list[UUID] = field(default_factory=list)
    released: list[UUID] = field(default_factory=list)
    unblocked: list[UUID] = field(default_factory=list)
    blocked: list[UUID] = field(default_factory=list)
    completed: list[UUID] = field(default_factory=list)


async def tick(env: Env) -> TickReport:
    """One idempotent pass over durable state; safe to run after every message or on a timer."""
    report = TickReport()
    await _promote_deferred(env, report)
    active = await env.store.list_objectives(["active"])
    await refresh_all(env, active)
    for objective in await env.store.list_objectives(["active"]):
        await _reconcile(env, objective, report)
    active = await env.store.list_objectives(["active"])
    await _release(env, active, report)
    await _escalate_stuck_waits(env, active)
    await manage_pressure(env)
    for objective in active:
        fresh = await env.store.get_objective(objective.id)
        if fresh is None:
            continue
        await _deadline_notice(env, fresh)
        if await evaluate_completion(env, fresh):
            report.completed.append(fresh.id)
    return report


async def _promote_deferred(env: Env, report: TickReport) -> None:
    deferred = await env.store.list_objectives(["deferred"])
    if not deferred:
        return
    slots = env.limits.max_active_objectives - await env.store.count_objectives(["active"])
    now = env.now()
    for objective in sorted(deferred, key=lambda item: (-item.priority, item.created_at)):
        if slots <= 0:
            return
        if objective.review_at is None or as_utc(objective.review_at) > now:
            continue
        changes = await env.store.objective_events(objective.id, "objective.status_changed")
        if not changes or changes[-1][2].get("reason") != LIMIT_DEFERRAL_REASON:
            continue  # deferred by a person: only a person resumes it
        await env.store.objectives.set_status(
            objective.id, "active", actor=ACTOR, reason="a slot opened for the deferred objective"
        )
        fresh = await env.store.get_objective(objective.id)
        if fresh is not None:
            await plan_objective(env, fresh)
        report.promoted.append(objective.id)
        slots -= 1


async def _reconcile(env: Env, objective: ObjectiveRecord, report: TickReport) -> None:
    tasks = await env.store.tasks_of(objective.id)
    dependencies = await env.store.dependencies_of(task.id for task in tasks)
    known = {task.id: TaskStatus(task.state) for task in tasks}
    outside = {dep for deps in dependencies.values() for dep in deps if dep not in known}
    for dep_id, state in (await env.store.states_of(outside)).items():
        known[dep_id] = TaskStatus(state)

    for task in tasks:
        state = TaskStatus(task.state)
        dep_states = [known[dep] for dep in dependencies[task.id] if dep in known]
        if state is TaskStatus.FAILED:
            await escalate_objective(
                env,
                objective,
                key=f"task:{task.id}",
                reason=f"task '{task.title}' failed after {task.attempt} attempts: "
                f"{(task.last_error or 'no error recorded')[:200]}",
                kind=TASK_FAILURE,
                task_id=task.id,
            )
            return
        if state in (TaskStatus.PENDING, TaskStatus.BLOCKED) and any(
            dep in _FAILED_DEPENDENCY for dep in dep_states
        ):
            if state is TaskStatus.PENDING:
                await _transition(env, task, TaskStatus.BLOCKED, "a dependency failed", report)
            continue
        if state is TaskStatus.BLOCKED:
            if dep_states and all(dep is TaskStatus.COMPLETED for dep in dep_states):
                await _transition(env, task, TaskStatus.PENDING, "blocker resolved", report)
            elif not dep_states and await _requeue_resource_block(env, task):
                await _transition(
                    env, task, TaskStatus.PENDING, "resource blocker requeued", report
                )


async def _escalate_requirement_waits(
    env: Env, objectives: dict[UUID, ObjectiveRecord], held: list[TaskRecord]
) -> None:
    """A FORGE task held for a requirement escalates once it waited ``requirement_wait_minutes``.

    Each wait round starts when the task is first seen held and ends with one escalation
    trigger; the escalation itself is deduplicated per (objective, kind) window.
    """
    if not held:
        return
    now = env.now()
    timeout = timedelta(minutes=env.limits.requirement_wait_minutes)
    for task in held:
        trigger = f"requirement-wait:{task.id}:"
        rounds = len(await env.store.keyed_decisions(f"orchestr:escalation-trigger:{trigger}"))
        marker = f"orchestr:requirement-wait:{task.id}:{rounds}"
        started = await env.store.get_decision(marker)
        if started is None:
            await env.store.record_decision(
                "requirement_wait",
                f"task '{task.title[:120]}' waits for an approved requirement",
                key=marker,
                evidence=[now.isoformat()],
                objective_id=task.objective_id,
                task_id=task.id,
            )
            continue
        try:
            since = as_utc(datetime.fromisoformat(started.evidence[0]))
        except (IndexError, ValueError):
            continue
        if now - since < timeout:
            continue
        await escalate_objective(
            env,
            objectives[task.objective_id],
            key=f"{trigger}{rounds}",
            reason=f"task '{task.title[:120]}' waited {env.limits.requirement_wait_minutes:g} "
            "minutes for an approved requirement; research found no adoptable approach",
            kind=REQUIREMENT,
            task_id=task.id,
        )


async def _wait_started(
    env: Env, episode: str, task: TaskRecord, what: str, now: datetime
) -> datetime | None:
    """When this wait episode began, recording it on first sight (``None`` on that first sight).

    The start is stored in the decision rather than read from the task row: decision rows and
    task rows are stamped by the database layer's own clock, while ORCHESTR's ``env.now`` is the
    clock its timeouts are measured with.
    """
    marker = f"orchestr:{episode}"
    started = await env.store.get_decision(marker)
    if started is None:
        await env.store.record_decision(
            "wait_started",
            f"task '{task.title[:120]}' {what}",
            key=marker,
            evidence=[now.isoformat()],
            objective_id=task.objective_id,
            task_id=task.id,
        )
        return None
    try:
        return as_utc(datetime.fromisoformat(started.evidence[0]))
    except (IndexError, ValueError):
        return None


async def _escalate_waits(
    env: Env,
    objectives: dict[UUID, ObjectiveRecord],
    tasks: list[TaskRecord],
    *,
    label: str,
    kind: str,
    minutes: float,
    what: str,
    decision: str,
) -> None:
    """Escalate each task once per wait episode when it waited longer than ``minutes``.

    An episode is one attempt of one task; leaving the wait and waiting again later (a new
    attempt) starts a new episode. The escalation is also deduplicated per (objective, kind)
    window and never blocks the objective: these waits resolve on their own or by a reply.
    """
    now = env.now()
    timeout = timedelta(minutes=minutes)
    for task in tasks:
        episode = f"{label}:{task.id}:{task.attempt}"
        if await env.store.get_decision(f"orchestr:escalation-trigger:{episode}") is not None:
            continue
        since = await _wait_started(env, episode, task, what, now)
        if since is None or now - since < timeout:
            continue
        await escalate_objective(
            env,
            objectives[task.objective_id],
            key=episode,
            reason=f"task '{task.title[:120]}' {what} for more than {minutes:g} minutes",
            kind=kind,
            task_id=task.id,
            block=False,
            decision=decision,
        )


async def _escalate_stuck_waits(env: Env, active: list[ObjectiveRecord]) -> None:
    """Tasks waiting for verification or an approval do not wait forever."""
    objectives = {objective.id: objective for objective in active}
    verification: list[TaskRecord] = []
    approval: list[TaskRecord] = []
    for objective in active:
        verification.extend(
            await env.store.tasks_of(objective.id, [TaskStatus.WAITING_FOR_VERIFICATION])
        )
        approval.extend(await env.store.tasks_of(objective.id, [TaskStatus.WAITING_FOR_APPROVAL]))
    await _escalate_waits(
        env,
        objectives,
        verification,
        label="verification-wait",
        kind=VERIFICATION_WAIT,
        minutes=env.limits.verification_wait_minutes,
        what="is waiting for its independent test run",
        decision="Check why verification has not finished, or cancel the task",
    )
    await _escalate_waits(
        env,
        objectives,
        approval,
        label="approval-wait",
        kind=APPROVAL_WAIT,
        minutes=env.limits.approval_wait_minutes,
        what="is waiting for an approval",
        decision="Approve or deny the pending request, or cancel the task",
    )


async def _escalate_resource_deferrals(
    env: Env,
    objectives: dict[UUID, ObjectiveRecord],
    by_id: dict[UUID, TaskRecord],
    deferred: Sequence[DeferredTask],
) -> None:
    """A ready task the scheduler keeps deferring for resources escalates with the reason."""
    now = env.now()
    timeout = timedelta(minutes=env.limits.resource_deferral_minutes)
    for item in deferred:
        task = by_id.get(item.task_id)
        if task is None:
            continue
        episode = f"resource-deferral:{task.id}:{task.attempt}"
        if await env.store.get_decision(f"orchestr:escalation-trigger:{episode}") is not None:
            continue
        since = await _wait_started(
            env, episode, task, f"is deferred for resources: {item.reason}", now
        )
        if since is None or now - since < timeout:
            continue
        await escalate_objective(
            env,
            objectives[task.objective_id],
            key=episode,
            reason=f"task '{task.title[:120]}' has been deferred for resources for more than "
            f"{env.limits.resource_deferral_minutes:g} minutes: {item.reason}",
            kind=RESOURCE_DEFERRAL,
            task_id=task.id,
            block=False,
            decision="Free up resources on the host, or cancel the task",
        )


async def _requeue_resource_block(env: Env, task: TaskRecord) -> bool:
    """A resource blocker has no resolver task; the task re-enters the queue exactly once."""
    for view in await env.store.decisions(task_id=task.id, decision="blocker"):
        if not view.evidence or view.evidence[0] != "resource":
            continue
        marker = f"orchestr:requeued:{view.key}"
        if await env.store.get_decision(marker) is None:
            await env.store.record_decision(
                "requeued",
                "resource-blocked task returned to the queue",
                key=marker,
                task_id=task.id,
            )
            return True
    return False


async def _transition(
    env: Env, task: TaskRecord, status: TaskStatus, reason: str, report: TickReport
) -> None:
    try:
        await env.store.tasks.transition(task.id, status, actor=ACTOR, details={"reason": reason})
    except InvalidTaskTransition:
        return
    (report.unblocked if status is TaskStatus.PENDING else report.blocked).append(task.id)


async def _release(env: Env, active: list[ObjectiveRecord], report: TickReport) -> None:
    objectives = {objective.id: objective for objective in active}
    pending: list[TaskRecord] = []
    for objective in active:
        pending.extend(await env.store.tasks_of(objective.id, [TaskStatus.PENDING]))
    if not pending:
        return
    dependencies = await env.store.dependencies_of(task.id for task in pending)
    dep_states = await env.store.states_of({dep for deps in dependencies.values() for dep in deps})
    ready = [
        task
        for task in pending
        if all(dep_states.get(dep) == TaskStatus.COMPLETED.value for dep in dependencies[task.id])
    ]
    held = await held_for_requirement(env, ready)
    await _escalate_requirement_waits(env, objectives, [task for task in ready if task.id in held])
    ready = [task for task in ready if task.id not in held]
    if not ready:
        return
    try:
        host = env.monitor.snapshot(env.disk_path)
    except Exception:
        env.logger.exception("resource snapshot failed; holding all releases this tick")
        return

    occupying = await env.store.occupying_tasks()
    running = len(occupying)
    builds = sum(1 for task in occupying if _is_build(task))
    by_id = {task.id: task for task in ready}
    now = env.now()
    candidates = {
        task.id: SchedulingCandidate(
            task_id=task.id,
            priority=task_rank(task.priority, objectives[task.objective_id]),
            waiting_since=as_utc(task.updated_at),
            deadline=as_utc(task.deadline) if task.deadline else None,
            estimated_ram_bytes=task.estimated_ram_mb * _MIB,
            estimated_disk_bytes=task.estimated_disk_mb * _MIB,
            estimated_cpu_percent=task.estimated_cpu_percent or _DEFAULT_CPU,
            is_build=_is_build(task),
        )
        for task in ready
    }
    first = True
    while candidates:
        decision = env.scheduler.choose_next(
            candidates.values(),
            SchedulerResources(host=host, running_tasks=running, running_builds=builds),
            now=now,
        )
        if first:
            first = False
            signature = f"{decision.selected_task_id}|" + "|".join(
                sorted(f"{item.task_id}:{item.reason}" for item in decision.deferred)
            )
            if signature != env.state.last_signature:
                env.state.last_signature = signature
                await env.scheduler.record_decision(decision)
        selected = decision.selected_task_id
        if selected is None:
            await _escalate_resource_deferrals(env, objectives, by_id, decision.deferred)
            return
        candidate = candidates.pop(selected)
        task = by_id[selected]
        if await release_task(env, task, objectives[task.objective_id]):
            report.released.append(task.id)
            running += 1
            builds += 1 if candidate.is_build else 0


def _is_build(task: TaskRecord) -> bool:
    return is_build(task)


async def _deadline_notice(env: Env, objective: ObjectiveRecord) -> None:
    if objective.deadline is None or objective.generated_by_agent:
        return
    hours = (as_utc(objective.deadline) - env.now()).total_seconds() / 3600
    await env.send_user_update(
        UpdateEvent(UpdateKind.DEADLINE_AT_RISK, hours_to_deadline=hours),
        key=f"deadline:{objective.id}",
        what_happened=f"The deadline for '{objective.title}' is approaching and work is not complete.",
        objective=objective,
        uncertain=["Whether the remaining work can finish before the deadline"],
    )
