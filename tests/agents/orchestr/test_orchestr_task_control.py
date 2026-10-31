"""ORCHESTR uses task control: objective cancellation and resource-pressure pause/resume."""

from uuid import UUID

import pytest
from orchestr_helpers import GIB, Harness
from sqlalchemy import select, update

from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import ObjectiveUpdatePayload
from aether.communication.messages import AgentId
from aether.runtime.control import (
    CONTROL_REQUESTED,
    TASK_CONTROL_SERVICE,
    ControlAction,
    TaskControl,
)
from aether.storage.models import EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories.tasks import TaskRepository


@pytest.fixture
def control(harness: Harness) -> TaskControl:
    service = TaskControl(harness.database.sessions, clock=harness.clock)
    harness.ctx.services[TASK_CONTROL_SERVICE] = service.bound_to("ORCHESTR")
    return service


async def _running(harness: Harness, objective_id: UUID, *keys: str) -> list[TaskRecord]:
    tasks = await harness.tasks(objective_id)
    for key in keys:
        for step in (TaskStatus.SCHEDULED, TaskStatus.RUNNING):
            await harness.store.tasks.transition(tasks[key].id, step, actor="test")
    return [tasks[key] for key in keys]


async def _control_events(harness: Harness, task_id: UUID) -> list[EventRecord]:
    async with harness.database.sessions() as session:
        rows = await session.scalars(
            select(EventRecord)
            .where(EventRecord.event_type == CONTROL_REQUESTED, EventRecord.aggregate_id == task_id)
            .order_by(EventRecord.created_at, EventRecord.id)
        )
        return list(rows)


async def _cancel(harness: Harness, objective_id: UUID, reason: str = "user cancelled") -> None:
    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason=reason, status="cancelled"),
    )


async def _worker_pauses(harness: Harness, control: TaskControl, task_id: UUID) -> None:
    """Play the worker pool: the running task yields, then the request is settled."""
    await harness.store.tasks.transition(task_id, TaskStatus.PAUSED, actor="worker")
    await control.settle(task_id)


async def _make_uninterruptible(harness: Harness, task_id: UUID) -> None:
    async with harness.database.sessions() as session, session.begin():
        await session.execute(
            update(TaskRecord).where(TaskRecord.id == task_id).values(interruptible=False)
        )


def _pressure(harness: Harness) -> None:
    harness.monitor.cpu = 99.0


def _calm(harness: Harness) -> None:
    harness.monitor.cpu = 5.0


async def _ticks(harness: Harness, count: int) -> None:
    for _ in range(count):
        harness.clock.advance(seconds=30)
        await harness.tick()


# --- objective cancellation ----------------------------------------------------------------


async def test_cancelling_an_objective_requests_cancel_for_running_tasks(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (running,) = await _running(harness, objective_id, "research.scout")

    await _cancel(harness, objective_id, "no longer needed")

    tasks = await harness.tasks(objective_id)
    assert await harness.state(running.id) is TaskStatus.RUNNING
    assert await control.pending_action(running.id) is ControlAction.CANCEL
    (event,) = await _control_events(harness, running.id)
    assert event.actor == "ORCHESTR"
    assert event.payload["action"] == "cancel"
    assert event.payload["requested_by"] == "ORCHESTR"
    assert "no longer needed" in event.payload["reason"]
    others = [task for key, task in tasks.items() if key != "research.scout"]
    assert others
    for task in others:
        assert await harness.state(task.id) is TaskStatus.CANCELLED
    decisions = await harness.store.decisions(task_id=running.id, decision="task_cancel_requested")
    assert decisions and "no longer needed" in decisions[0].rationale


async def test_cancelling_without_task_control_leaves_running_tasks_and_logs_it(
    harness: Harness,
) -> None:
    objective_id = await harness.objective()
    (running,) = await _running(harness, objective_id, "research.scout")

    await _cancel(harness, objective_id)

    assert await harness.state(running.id) is TaskStatus.RUNNING
    assert await harness.store.decisions(task_id=running.id, decision="task_cancel_unavailable")


async def test_a_task_leased_while_its_objective_is_cancelled_still_gets_a_cancel_request(
    harness: Harness, control: TaskControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    target = tasks["research.scout"]
    await harness.store.tasks.transition(target.id, TaskStatus.SCHEDULED, actor="test")
    original = TaskRepository.transition

    async def worker_leases_first(self, task_id, new_status, **kwargs):
        if task_id == target.id and new_status is TaskStatus.CANCELLED:
            # The worker pool leased the task between ORCHESTR's read and its move.
            await original(self, task_id, TaskStatus.RUNNING, actor="worker")
        return await original(self, task_id, new_status, **kwargs)

    monkeypatch.setattr(TaskRepository, "transition", worker_leases_first)

    await _cancel(harness, objective_id, "no longer needed")

    assert await harness.state(target.id) is TaskStatus.RUNNING
    assert await control.pending_action(target.id) is ControlAction.CANCEL
    assert await harness.store.decisions(task_id=target.id, decision="task_cancel_requested")


async def test_cancelling_is_not_repeated_for_the_same_message(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (running,) = await _running(harness, objective_id, "research.scout")
    message = await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="stop", status="cancelled"),
    )
    await harness.agent.dispatch_message(message, harness.ctx)

    assert len(await _control_events(harness, running.id)) == 1


# --- resource pressure ---------------------------------------------------------------------


async def test_sustained_pressure_pauses_one_interruptible_running_task(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    scout, rogue = await _running(harness, objective_id, "research.scout", "research.rogue")

    _pressure(harness)
    await _ticks(harness, 2)
    assert not await _control_events(harness, scout.id)
    assert not await _control_events(harness, rogue.id)

    await _ticks(harness, 1)
    requests = [
        event for task in (scout, rogue) for event in await _control_events(harness, task.id)
    ]
    assert len(requests) == 1
    assert requests[0].actor == "ORCHESTR"
    assert requests[0].payload["action"] == "pause"
    assert requests[0].payload["reason"].startswith("resource pressure: cpu 99%")
    paused_id = requests[0].aggregate_id
    decisions = await harness.store.decisions(task_id=paused_id, decision="pressure_pause")
    assert len(decisions) == 1


async def test_pressure_below_the_streak_never_pauses(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    scout, _ = await _running(harness, objective_id, "research.scout", "research.rogue")

    for _ in range(3):
        _pressure(harness)
        await _ticks(harness, 2)
        _calm(harness)
        await _ticks(harness, 1)

    assert not await _control_events(harness, scout.id)


async def test_busy_but_under_the_ceiling_host_is_not_pressure(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    scout, _ = await _running(harness, objective_id, "research.scout", "research.rogue")

    harness.monitor.cpu = 60.0  # whole-host percent; the safety ceiling is 85
    await _ticks(harness, 6)

    assert not await _control_events(harness, scout.id)


async def test_low_ram_and_low_disk_count_as_pressure(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")
    harness.use_limits(OrchestrLimits(pressure_pause_after_ticks=1))

    harness.monitor.ram = 64 * 1024 * 1024
    await _ticks(harness, 1)
    (event,) = await _control_events(harness, scout.id)
    assert "available ram 64 MiB" in event.payload["reason"]

    harness.monitor.ram = 16 * GIB
    harness.monitor.free_disk = 1 * GIB
    await _worker_pauses(harness, control, scout.id)
    await harness.store.tasks.transition(scout.id, TaskStatus.READY, actor="test")
    for step in (TaskStatus.SCHEDULED, TaskStatus.RUNNING):
        await harness.store.tasks.transition(scout.id, step, actor="test")
    await _ticks(harness, 1)
    events = await _control_events(harness, scout.id)
    assert "free disk" in events[-1].payload["reason"]


async def test_non_interruptible_and_orchestr_work_is_never_paused(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    scout, rogue = await _running(harness, objective_id, "research.scout", "research.rogue")
    await _make_uninterruptible(harness, scout.id)
    await _make_uninterruptible(harness, rogue.id)

    _pressure(harness)
    await _ticks(harness, 5)

    assert not await _control_events(harness, scout.id)
    assert not await _control_events(harness, rogue.id)


async def test_pause_limit_per_tick_and_pending_requests_are_respected(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    scout, rogue = await _running(harness, objective_id, "research.scout", "research.rogue")
    harness.use_limits(OrchestrLimits(pressure_pause_after_ticks=1, pressure_max_pauses_per_tick=1))

    _pressure(harness)
    await _ticks(harness, 1)
    first = [len(await _control_events(harness, task.id)) for task in (scout, rogue)]
    assert sorted(first) == [0, 1]

    await _ticks(harness, 1)
    second = [len(await _control_events(harness, task.id)) for task in (scout, rogue)]
    assert second == [1, 1]  # the already-asked task is not asked again

    await _ticks(harness, 3)
    assert [len(await _control_events(harness, task.id)) for task in (scout, rogue)] == [1, 1]


async def test_pressure_pause_is_disabled_with_zero_limit(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")
    harness.use_limits(OrchestrLimits(pressure_max_pauses_per_tick=0))

    _pressure(harness)
    await _ticks(harness, 6)

    assert not await _control_events(harness, scout.id)


async def test_pressure_is_ignored_without_task_control(harness: Harness) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")

    _pressure(harness)
    await _ticks(harness, 6)

    assert await harness.state(scout.id) is TaskStatus.RUNNING
    assert not await _control_events(harness, scout.id)


async def test_recovery_resumes_tasks_paused_by_pressure(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")
    harness.use_limits(OrchestrLimits(pressure_pause_after_ticks=2, pressure_resume_after_ticks=2))

    _pressure(harness)
    await _ticks(harness, 2)
    assert await control.pending_action(scout.id) is ControlAction.PAUSE
    await _worker_pauses(harness, control, scout.id)
    assert await harness.state(scout.id) is TaskStatus.PAUSED

    await _ticks(harness, 1)
    assert await harness.state(scout.id) is TaskStatus.PAUSED  # still under pressure

    _calm(harness)
    await _ticks(harness, 1)
    assert await harness.state(scout.id) is TaskStatus.PAUSED  # not calm for long enough

    await _ticks(harness, 1)
    assert await harness.state(scout.id) is TaskStatus.READY
    resumed = await harness.store.decisions(task_id=scout.id, decision="pressure_resume")
    assert len(resumed) == 1
    assert "resources recovered" in resumed[0].rationale
    actions = [event.payload["action"] for event in await _control_events(harness, scout.id)]
    assert actions == ["pause", "resume"]


async def test_recovery_resumes_one_task_per_tick(harness: Harness, control: TaskControl) -> None:
    objective_id = await harness.objective()
    scout, rogue = await _running(harness, objective_id, "research.scout", "research.rogue")
    harness.use_limits(
        OrchestrLimits(
            pressure_pause_after_ticks=1,
            pressure_resume_after_ticks=1,
            pressure_max_pauses_per_tick=2,
        )
    )

    _pressure(harness)
    await _ticks(harness, 1)
    for task in (scout, rogue):
        await _worker_pauses(harness, control, task.id)

    _calm(harness)
    await _ticks(harness, 1)
    states = {await harness.state(task.id) for task in (scout, rogue)}
    assert states == {TaskStatus.READY, TaskStatus.PAUSED}

    await _ticks(harness, 1)
    states = {await harness.state(task.id) for task in (scout, rogue)}
    assert states == {TaskStatus.READY}


async def test_tasks_paused_by_someone_else_are_not_resumed_on_recovery(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")
    await control.bound_to("USER").request_pause(scout.id, reason="user asked")
    await _worker_pauses(harness, control, scout.id)

    _calm(harness)
    await _ticks(harness, 5)

    assert await harness.state(scout.id) is TaskStatus.PAUSED


async def test_recovery_does_not_resume_tasks_of_an_inactive_objective(
    harness: Harness, control: TaskControl
) -> None:
    objective_id = await harness.objective()
    (scout,) = await _running(harness, objective_id, "research.scout")
    harness.use_limits(OrchestrLimits(pressure_pause_after_ticks=1, pressure_resume_after_ticks=1))

    _pressure(harness)
    await _ticks(harness, 1)
    await _worker_pauses(harness, control, scout.id)
    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="hold", status="paused"),
    )

    _calm(harness)
    await _ticks(harness, 3)

    assert await harness.state(scout.id) is TaskStatus.PAUSED
