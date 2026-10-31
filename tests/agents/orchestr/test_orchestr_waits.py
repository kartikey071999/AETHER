"""Bounded waits (T-051): verification, approval and resource deferral escalate once."""

from uuid import UUID

from orchestr_helpers import GIB, Harness

from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus


def _muse(harness: Harness) -> list:
    return harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)


async def _escalations(harness: Harness) -> list:
    return await harness.store.keyed_decisions("orchestr:escalated:")


async def _wait_in(harness: Harness, state: TaskStatus) -> tuple[UUID, UUID]:
    objective_id = await harness.objective()
    task = (await harness.tasks(objective_id))["research.scout"]
    await harness.run_to(task.id, state)
    await harness.tick()
    return objective_id, task.id


async def _many_ticks(harness: Harness, count: int, *, minutes: float = 30) -> None:
    for _ in range(count):
        harness.clock.advance(minutes=minutes)
        await harness.tick()


async def test_verification_wait_escalates_once_after_the_bound(harness: Harness) -> None:
    objective_id, task_id = await _wait_in(harness, TaskStatus.WAITING_FOR_VERIFICATION)

    harness.clock.advance(minutes=29)
    await harness.tick()
    assert _muse(harness) == []

    harness.clock.advance(minutes=2)
    await harness.tick()
    await _many_ticks(harness, 6, minutes=45)

    [escalation] = await _escalations(harness)
    assert escalation.evidence[0] == "verification_wait"
    assert "independent test run" in escalation.rationale
    assert len(_muse(harness)) == 1
    assert (await harness.get_objective(objective_id)).status == "active"
    assert await harness.state(task_id) is TaskStatus.WAITING_FOR_VERIFICATION


async def test_approval_wait_escalates_once_after_its_own_bound(harness: Harness) -> None:
    objective_id, task_id = await _wait_in(harness, TaskStatus.WAITING_FOR_APPROVAL)

    harness.clock.advance(minutes=31)
    await harness.tick()
    assert _muse(harness) == []  # the approval bound is longer than the verification one

    harness.clock.advance(minutes=30)
    await harness.tick()
    await _many_ticks(harness, 6, minutes=45)

    [escalation] = await _escalations(harness)
    assert escalation.evidence[0] == "approval_wait"
    assert "approval" in escalation.rationale
    assert len(_muse(harness)) == 1
    assert (await harness.get_objective(objective_id)).status == "active"
    assert await harness.state(task_id) is TaskStatus.WAITING_FOR_APPROVAL


async def test_wait_bounds_are_configurable(harness: Harness) -> None:
    harness.use_limits(
        OrchestrLimits(
            llm_refinement=False, verification_wait_minutes=1.0, approval_wait_minutes=2.0
        )
    )
    await _wait_in(harness, TaskStatus.WAITING_FOR_VERIFICATION)
    harness.clock.advance(minutes=1.5)
    await harness.tick()
    assert len(_muse(harness)) == 1


async def test_a_task_that_leaves_the_wait_does_not_escalate(harness: Harness) -> None:
    _, task_id = await _wait_in(harness, TaskStatus.WAITING_FOR_VERIFICATION)
    harness.clock.advance(minutes=20)
    await harness.tick()

    await harness.store.tasks.transition(task_id, TaskStatus.CANCELLED, actor="test")
    await _many_ticks(harness, 4)

    assert await _escalations(harness) == []
    assert _muse(harness) == []


async def test_verification_and_approval_waits_escalate_independently(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    await harness.run_to(tasks["research.scout"].id, TaskStatus.WAITING_FOR_VERIFICATION)
    await harness.run_to(tasks["research.rogue"].id, TaskStatus.WAITING_FOR_APPROVAL)
    await harness.tick()

    await _many_ticks(harness, 4, minutes=30)

    kinds = sorted(item.evidence[0] for item in await _escalations(harness))
    assert kinds == ["approval_wait", "verification_wait"]
    assert len(_muse(harness)) == 2


async def test_terminal_objective_waits_are_not_escalated(harness: Harness) -> None:
    objective_id, _ = await _wait_in(harness, TaskStatus.WAITING_FOR_VERIFICATION)
    await harness.store.objectives.set_status(
        objective_id, "cancelled", actor="MUSE", reason="user cancelled"
    )
    await _many_ticks(harness, 4)
    assert await _escalations(harness) == []


async def _finish_research(harness: Harness, objective_id: UUID) -> dict:
    tasks = await harness.tasks(objective_id)
    await harness.finish(tasks["research.scout"].id)
    await harness.finish(tasks["research.rogue"].id)
    return tasks


async def test_resource_deferral_escalates_once_with_the_reason(harness: Harness) -> None:
    objective_id = await harness.objective()
    harness.monitor.free_disk = 1 * GIB
    tasks = await _finish_research(harness, objective_id)
    consolidation = tasks["consolidation"].id
    assert await harness.state(consolidation) is TaskStatus.PENDING

    harness.clock.advance(minutes=14)
    await harness.tick()
    assert _muse(harness) == []

    harness.clock.advance(minutes=2)
    await harness.tick()
    await _many_ticks(harness, 6, minutes=20)

    [escalation] = await _escalations(harness)
    assert escalation.evidence[0] == "resource_deferral"
    assert "free-disk reserve" in escalation.rationale
    assert len(_muse(harness)) == 1
    assert await harness.state(consolidation) is TaskStatus.PENDING
    assert (await harness.get_objective(objective_id)).status == "active"


async def test_resource_deferral_that_clears_does_not_escalate(harness: Harness) -> None:
    objective_id = await harness.objective()
    harness.monitor.free_disk = 1 * GIB
    tasks = await _finish_research(harness, objective_id)

    harness.clock.advance(minutes=10)
    await harness.tick()
    harness.monitor.free_disk = 500 * GIB
    await harness.tick()
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.READY

    await _many_ticks(harness, 4)
    assert await _escalations(harness) == []


async def test_resource_deferral_bound_is_configurable(harness: Harness) -> None:
    harness.use_limits(OrchestrLimits(llm_refinement=False, resource_deferral_minutes=1.0))
    objective_id = await harness.objective()
    harness.monitor.free_disk = 1 * GIB
    await _finish_research(harness, objective_id)

    harness.clock.advance(minutes=2)
    await harness.tick()
    assert len(_muse(harness)) == 1
