"""Authority boundaries, task creation limits, restart safety, and the factory wiring."""

import pytest
from orchestr_helpers import Harness

from aether.agents.factory import resolve_agent_class
from aether.agents.orchestr import OrchestrAgent
from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import (
    ObjectiveCreatePayload,
    ObjectiveUpdatePayload,
    ResourceEstimate,
    TaskCreatePayload,
)
from aether.communication.messages import AgentId
from aether.storage.models import TaskStatus


def _create(objective_id, agent=AgentId.SCOUT, **fields) -> TaskCreatePayload:
    return TaskCreatePayload(
        objective_id=objective_id, title="Extra work", suggested_agent=agent, **fields
    )


async def test_factory_resolves_the_real_agent() -> None:
    assert resolve_agent_class(AgentId.ORCHESTR) is OrchestrAgent


@pytest.mark.parametrize("source", [AgentId.SCOUT, AgentId.ROGUE, AgentId.LEDGER])
async def test_research_agents_cannot_cancel_or_pause_objectives(
    harness: Harness, source: AgentId
) -> None:
    objective_id = await harness.objective()
    for status in ("cancelled", "paused", "blocked"):
        await harness.send(
            source,
            ObjectiveUpdatePayload(objective_id=objective_id, reason="nope", status=status),
        )
    assert (await harness.get_objective(objective_id)).status == "active"
    tasks = await harness.tasks(objective_id)
    assert await harness.state(tasks["research.scout"].id) is TaskStatus.READY


async def test_muse_cancel_cancels_open_tasks(harness: Harness) -> None:
    objective_id = await harness.objective()
    await harness.send(
        AgentId.MUSE,
        ObjectiveUpdatePayload(objective_id=objective_id, reason="user", status="cancelled"),
    )
    assert (await harness.get_objective(objective_id)).status == "cancelled"
    states = {task.state for task in (await harness.tasks(objective_id)).values()}
    assert states == {TaskStatus.CANCELLED.value}


async def test_completion_cannot_be_forced_by_update(harness: Harness) -> None:
    objective_id = await harness.objective()
    with pytest.raises(Exception, match="objective.completed"):
        ObjectiveUpdatePayload(objective_id=objective_id, reason="done", status="completed")


async def test_task_create_for_workers_is_accepted(harness: Harness) -> None:
    objective_id = await harness.objective()
    message = await harness.send(AgentId.SCOUT, _create(objective_id))
    created = await harness.store.task_by_key(f"task.create:{message.message_id}")
    assert created is not None
    assert created.assigned_agent == AgentId.SCOUT.value


@pytest.mark.parametrize("target", [AgentId.WARDEN, AgentId.MUSE, AgentId.ORCHESTR, None])
async def test_task_create_cannot_target_control_agents(
    harness: Harness, target: AgentId | None
) -> None:
    objective_id = await harness.objective()
    before = await harness.store.count_tasks(objective_id)
    await harness.send(AgentId.ROGUE, _create(objective_id, target))
    assert await harness.store.count_tasks(objective_id) == before
    rejected = await harness.store.decisions(decision="task_rejected")
    assert rejected


async def test_task_create_redelivery_is_idempotent(harness: Harness) -> None:
    objective_id = await harness.objective()
    message = await harness.send(AgentId.SCOUT, _create(objective_id))
    before = await harness.store.count_tasks(objective_id)
    await harness.agent.dispatch_message(message, harness.ctx)
    assert await harness.store.count_tasks(objective_id) == before


async def test_task_limit_per_objective_is_enforced(harness: Harness) -> None:
    objective_id = await harness.objective()
    existing = await harness.store.count_tasks(objective_id)
    harness.use_limits(OrchestrLimits(max_tasks_per_objective=existing + 1))
    await harness.send(AgentId.SCOUT, _create(objective_id))
    await harness.send(AgentId.SCOUT, _create(objective_id))
    assert await harness.store.count_tasks(objective_id) == existing + 1


async def test_task_dependencies_must_belong_to_the_objective(harness: Harness) -> None:
    first = await harness.objective("Add a parser capability")
    second = await harness.objective("Add a renderer capability")
    foreign = (await harness.tasks(second))["research.scout"]
    before = await harness.store.count_tasks(first)
    await harness.send(AgentId.SCOUT, _create(first, depends_on=[foreign.id]))
    assert await harness.store.count_tasks(first) == before


async def test_forge_tasks_depend_on_the_security_review(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    message = await harness.send(
        AgentId.SCOUT,
        _create(objective_id, AgentId.FORGE, resource_estimate=ResourceEstimate(ram_mb=256)),
    )
    created = await harness.store.task_by_key(f"task.create:{message.message_id}")
    assert created is not None
    deps = await harness.store.dependencies_of([created.id])
    assert tasks["security_review"].id in deps[created.id]
    assert await harness.state(created.id) is TaskStatus.PENDING


async def test_a_new_instance_resumes_from_the_database(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    await harness.run_to(tasks["research.scout"].id)
    await harness.run_to(tasks["research.rogue"].id)

    harness.restart()
    await harness.tick()
    assert await harness.state(tasks["consolidation"].id) is TaskStatus.READY
    assert len(await harness.tasks(objective_id)) == len(tasks)


async def test_objective_create_redelivery_does_not_duplicate(harness: Harness) -> None:
    payload = ObjectiveCreatePayload(
        title="Add a PDF parsing capability", completion_criteria=["Text is extracted"]
    )
    message = await harness.send(AgentId.MUSE, payload)
    objective_id = await harness.find_objective(payload.title)
    sent = len(harness.bus.messages)
    harness.restart()
    await harness.agent.dispatch_message(message, harness.ctx)
    assert len(await harness.store.list_objectives(["active"])) == 1
    assert len(await harness.tasks(objective_id)) == 6
    assert len(harness.bus.messages) == sent
