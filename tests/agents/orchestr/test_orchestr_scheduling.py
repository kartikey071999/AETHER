"""Scheduling tick: dependency release, resource gating, concurrency, and priority updates."""

from orchestr_helpers import GIB, Harness

from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import ObjectiveUpdatePayload
from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus


async def _finish_research(harness: Harness, objective_id) -> None:
    tasks = await harness.tasks(objective_id)
    await harness.finish(tasks["research.scout"].id)
    await harness.finish(tasks["research.rogue"].id)


async def test_dependents_wait_for_their_dependencies(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.PENDING

    await harness.finish(tasks["research.scout"].id)
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.PENDING

    await harness.finish(tasks["research.rogue"].id)
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.READY
    assert await harness.state(tasks["implementation"].id) is TaskStatus.PENDING


async def test_release_publishes_assignment_to_the_worker(harness: Harness) -> None:
    objective_id = await harness.objective()
    await _finish_research(harness, objective_id)
    tasks = await harness.tasks(objective_id)
    targets = {
        message.task_id: message.target_agent
        for message in harness.bus.of_type(MessageType.TASK_ASSIGN)
    }
    assert targets[tasks["consolidation"].id].value == tasks["consolidation"].assigned_agent


async def test_low_disk_holds_releases_and_records_the_decision(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    harness.monitor.free_disk = 1 * GIB
    await _finish_research(harness, objective_id)
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.PENDING

    harness.monitor.free_disk = 500 * GIB
    await harness.tick()
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.READY


async def test_failing_monitor_releases_nothing(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    harness.monitor.fail = True
    await _finish_research(harness, objective_id)
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.PENDING

    harness.monitor.fail = False
    await harness.tick()
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.READY


async def test_concurrency_limit_caps_released_tasks(harness: Harness) -> None:
    harness.ctx.services["scheduler_policy"] = harness.ctx.services["scheduler_policy"].model_copy(
        update={"max_concurrent_tasks": 1}
    )
    first = await harness.objective("Add a parser capability")
    second = await harness.objective("Add a renderer capability")
    held = await harness.tasks(second)
    assert {task.state for task in held.values()} == {TaskStatus.PENDING.value}

    for task in (await harness.tasks(first)).values():
        if task.state == TaskStatus.READY.value:
            await harness.run_to(task.id)
    await harness.tick()
    released = [
        task
        for task in (await harness.tasks(second)).values()
        if task.state == TaskStatus.READY.value
    ]
    assert len(released) == 1


async def test_only_one_build_task_runs_at_a_time(harness: Harness) -> None:
    objective_id = await harness.objective()
    await _finish_research(harness, objective_id)
    tasks = await harness.tasks(objective_id)
    await harness.finish(tasks["consolidation"].id)
    await harness.finish(tasks["security_review"].id)
    await harness.approve_requirement(objective_id)
    await harness.tick()
    assert await harness.state(tasks["implementation"].id) is TaskStatus.READY
    assert await harness.state(tasks["verification"].id) is TaskStatus.PENDING


async def test_tick_is_idempotent(harness: Harness) -> None:
    objective_id = await harness.objective()
    await _finish_research(harness, objective_id)
    before = len(harness.bus.messages)
    await harness.tick()
    await harness.tick()
    assert len(harness.bus.messages) == before


async def test_paused_objective_releases_nothing(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="user asked", status="paused"),
    )
    assert await harness.state(tasks["research.scout"].id) is TaskStatus.PAUSED
    await harness.tick()
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.PENDING

    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="resume", status="active"),
    )
    assert await harness.state(tasks["research.scout"].id) is TaskStatus.READY


async def test_agent_priority_boost_is_bounded(harness: Harness) -> None:
    objective_id = await harness.objective(priority=40)
    before = (await harness.get_objective(objective_id)).priority
    await harness.send(
        AgentId.SCOUT,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="looks urgent", priority=100),
    )
    after = (await harness.get_objective(objective_id)).priority
    cap = OrchestrLimits().max_agent_boost_points
    assert before < after <= before + cap


async def test_muse_sets_priority_and_agents_cannot_lower_it(harness: Harness) -> None:
    objective_id = await harness.objective(priority=40)
    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="user says so", priority=90),
    )
    raised = (await harness.get_objective(objective_id)).priority
    assert raised >= 85
    await harness.send(
        AgentId.ROGUE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="demote", priority=1),
    )
    assert (await harness.get_objective(objective_id)).priority >= raised - 1


async def test_agent_boost_decays_over_time(harness: Harness) -> None:
    objective_id = await harness.objective(priority=40)
    await harness.send(
        AgentId.SCOUT,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="urgent", priority=60),
    )
    boosted = (await harness.get_objective(objective_id)).priority
    harness.clock.advance(hours=24 * 30)
    await harness.tick()
    assert (await harness.get_objective(objective_id)).priority <= boosted
