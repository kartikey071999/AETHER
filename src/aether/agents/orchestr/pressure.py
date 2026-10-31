"""Sustained resource pressure: pause interruptible running work, resume it on recovery.

Deterministic: pressure is judged from one host snapshot per tick against the scheduler's own
limits, a pause needs ``pressure_pause_after_ticks`` consecutive pressured ticks and a resume
``pressure_resume_after_ticks`` consecutive calm ticks. Only running tasks flagged interruptible
(never builds, never ORCHESTR's own work) are paused, lowest priority first. Pause requests go
through the task-control service; ORCHESTR resumes only tasks whose latest control request is
its own pressure pause, so work paused by a person or an objective is never resumed here.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planner import StepKind, kind_of_idempotency_key
from aether.agents.orchestr.store import ACTOR
from aether.communication.messages import AgentId
from aether.runtime.control import CONTROL_REQUESTED, ControlAction
from aether.runtime.resource_monitor import HostResources
from aether.storage.models import EventRecord, TaskRecord, TaskStatus

PRESSURE_REASON_PREFIX = "resource pressure"
_MIB = 1024 * 1024


def pressure_reason(env: Env, host: HostResources) -> str | None:
    policy = env.scheduler.policy
    problems: list[str] = []
    if host.cpu_percent > policy.max_cpu_percent:
        problems.append(f"cpu {host.cpu_percent:.0f}% above limit {policy.max_cpu_percent:.0f}%")
    if host.free_disk_bytes < policy.min_free_disk_bytes:
        problems.append(
            f"free disk {host.free_disk_bytes // _MIB} MiB below reserve "
            f"{policy.min_free_disk_bytes // _MIB} MiB"
        )
    minimum_ram = env.limits.pressure_min_available_ram_mb
    if host.available_ram_bytes < minimum_ram * _MIB:
        problems.append(
            f"available ram {host.available_ram_bytes // _MIB} MiB below {minimum_ram} MiB"
        )
    if not problems:
        return None
    return f"{PRESSURE_REASON_PREFIX}: " + "; ".join(problems)


async def manage_pressure(env: Env) -> None:
    limits = env.limits
    if env.control is None or limits.pressure_max_pauses_per_tick == 0:
        return
    try:
        host = env.monitor.snapshot(env.disk_path)
    except Exception:
        env.logger.exception("resource snapshot failed; pressure state unchanged this tick")
        return
    reason = pressure_reason(env, host)
    state = env.state
    if reason is not None:
        state.pressured_ticks += 1
        state.calm_ticks = 0
        if state.pressured_ticks >= limits.pressure_pause_after_ticks:
            await _pause(env, reason)
    else:
        state.calm_ticks += 1
        state.pressured_ticks = 0
        if state.calm_ticks >= limits.pressure_resume_after_ticks:
            await _resume(env)


async def _pause(env: Env, reason: str) -> None:
    assert env.control is not None
    async with env.store.sessions() as session:
        running = list(
            await session.scalars(
                select(TaskRecord).where(TaskRecord.state == TaskStatus.RUNNING.value)
            )
        )
    candidates = sorted(
        (task for task in running if _pausable(task)),
        key=lambda task: (task.priority, task.created_at, str(task.id)),
    )
    paused = 0
    for task in candidates:
        if paused >= env.limits.pressure_max_pauses_per_tick:
            break
        if await env.control.pending_action(task.id) is not None:
            continue
        sustained = f"{reason} for {env.state.pressured_ticks} ticks"
        result = await env.control.request_pause(task.id, reason=sustained)
        paused += 1
        await env.store.record_decision(
            "pressure_pause",
            sustained,
            key=f"orchestr:pressure-pause:{task.id}:{env.now().isoformat()}",
            evidence=[result.outcome, task.title[:100]],
            objective_id=task.objective_id,
            task_id=task.id,
        )


def is_build(task: TaskRecord) -> bool:
    return task.assigned_agent == AgentId.FORGE.value or (
        kind_of_idempotency_key(task.idempotency_key) is StepKind.IMPLEMENTATION
    )


def _pausable(task: TaskRecord) -> bool:
    return (
        task.interruptible and task.assigned_agent != AgentId.ORCHESTR.value and not is_build(task)
    )


async def _resume(env: Env) -> None:
    """Resume one pressure-paused task per tick, highest priority first."""
    assert env.control is not None
    async with env.store.sessions() as session:
        paused = list(
            await session.scalars(
                select(TaskRecord).where(TaskRecord.state == TaskStatus.PAUSED.value)
            )
        )
        owned = [task for task in paused if await _paused_by_pressure(session, task)]
    for task in sorted(owned, key=lambda item: (-item.priority, item.created_at, str(item.id))):
        objective = await env.store.get_objective(task.objective_id)
        if objective is None or objective.status != "active":
            continue
        note = "resources recovered"
        if await env.control.resume(task.id, reason=note):
            await env.store.record_decision(
                "pressure_resume",
                f"{note} for {env.state.calm_ticks} ticks",
                key=f"orchestr:pressure-resume:{task.id}:{env.now().isoformat()}",
                evidence=[task.title[:100]],
                objective_id=task.objective_id,
                task_id=task.id,
            )
            return


async def _paused_by_pressure(session: AsyncSession, task: TaskRecord) -> bool:
    latest = await session.scalar(
        select(EventRecord)
        .where(
            EventRecord.event_type == CONTROL_REQUESTED,
            EventRecord.aggregate_type == "task",
            EventRecord.aggregate_id == task.id,
        )
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
        .limit(1)
    )
    if latest is None or latest.actor != ACTOR or not isinstance(latest.payload, dict):
        return False
    return latest.payload.get("action") == ControlAction.PAUSE.value and str(
        latest.payload.get("reason", "")
    ).startswith(PRESSURE_REASON_PREFIX)
