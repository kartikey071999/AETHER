"""The scheduling tick: promote, re-prioritize, reconcile, release, detect completion."""

from dataclasses import dataclass, field
from uuid import UUID

from aether.agents.orchestr.assignment import release_task
from aether.agents.orchestr.blockers import escalate_objective
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind
from aether.agents.orchestr.completion import evaluate_completion
from aether.agents.orchestr.env import Env, as_utc
from aether.agents.orchestr.intake import LIMIT_DEFERRAL_REASON
from aether.agents.orchestr.linking import held_for_requirement
from aether.agents.orchestr.planning import plan_objective
from aether.agents.orchestr.pressure import is_build, manage_pressure
from aether.agents.orchestr.priorities import refresh_all, task_rank
from aether.agents.orchestr.store import ACTOR
from aether.runtime.scheduler import SchedulerResources, SchedulingCandidate
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
