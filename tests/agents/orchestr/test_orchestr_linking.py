"""ORCHESTR links approved requirements to FORGE and LEDGER tasks and gates implementation (T-042)."""

from uuid import UUID

from orchestr_helpers import Harness
from sqlalchemy import select

from aether.communication.contracts import (
    ConsolidationDecision,
    FindingDecision,
    ResearchConsolidatedPayload,
)
from aether.communication.messages import AgentId
from aether.storage.models import EventRecord, RequirementStatus, TaskStatus
from aether.storage.repositories import RequirementRepository


async def _reach_implementation(harness: Harness, objective_id: UUID) -> dict:
    tasks = await harness.tasks(objective_id)
    for key in ("research.scout", "research.rogue", "consolidation", "security_review"):
        await harness.finish(tasks[key].id)
    return await harness.tasks(objective_id)


async def _consolidate(harness: Harness, objective_id: UUID, requirement_id: UUID) -> None:
    await harness.send(
        AgentId.SCOUT,
        ResearchConsolidatedPayload(
            objective_id=objective_id,
            decisions=[
                FindingDecision(
                    finding_id=requirement_id,
                    decision=ConsolidationDecision.ADOPT,
                    rationale="best approach",
                )
            ],
            requirement_ids=[requirement_id],
        ),
        objective_id=objective_id,
    )


async def _link_events(harness: Harness) -> list[EventRecord]:
    async with harness.database.sessions() as session:
        return list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "task.requirement_linked")
            )
        )


async def test_approved_requirement_is_linked_to_forge_and_ledger_tasks(harness: Harness) -> None:
    objective_id = await harness.objective()
    requirement_id = await harness.approve_requirement(objective_id)

    tasks = await harness.tasks(objective_id)
    assert tasks["implementation"].requirement_id == requirement_id
    assert tasks["verification"].requirement_id == requirement_id
    assert tasks["consolidation"].requirement_id is None
    assert tasks["research.scout"].requirement_id is None
    assert len(await _link_events(harness)) == 2


async def test_linking_is_idempotent_on_redelivery(harness: Harness) -> None:
    objective_id = await harness.objective()
    requirement_id = await harness.propose_requirement(objective_id, approved=True)
    await harness.evaluate_requirement(objective_id, requirement_id)
    await harness.evaluate_requirement(objective_id, requirement_id)
    await _consolidate(harness, objective_id, requirement_id)

    assert len(await _link_events(harness)) == 2
    tasks = await harness.tasks(objective_id)
    assert tasks["implementation"].requirement_id == requirement_id


async def test_consolidation_links_the_adopted_requirement(harness: Harness) -> None:
    objective_id = await harness.objective()
    requirement_id = await harness.propose_requirement(objective_id)
    await _consolidate(harness, objective_id, requirement_id)

    tasks = await harness.tasks(objective_id)
    assert tasks["implementation"].requirement_id == requirement_id
    assert tasks["verification"].requirement_id == requirement_id


async def test_foreign_or_rejected_requirements_are_not_linked(harness: Harness) -> None:
    objective_id = await harness.objective()
    other = await harness.objective("Add a renderer capability")
    foreign = await harness.propose_requirement(other)
    await _consolidate(harness, objective_id, foreign)

    rejected = await harness.propose_requirement(objective_id)
    await RequirementRepository(harness.database.sessions).set_status(
        rejected, RequirementStatus.REJECTED, actor="LEDGER"
    )
    await _consolidate(harness, objective_id, rejected)

    tasks = await harness.tasks(objective_id)
    assert tasks["implementation"].requirement_id is None
    assert tasks["verification"].requirement_id is None


async def test_implementation_waits_for_an_approved_requirement(harness: Harness) -> None:
    objective_id = await harness.objective()
    tasks = await _reach_implementation(harness, objective_id)
    assert await harness.state(tasks["implementation"].id) is TaskStatus.PENDING

    # Linked by consolidation but still PROPOSED: not yet authorized.
    requirement_id = await harness.propose_requirement(objective_id)
    await _consolidate(harness, objective_id, requirement_id)
    await harness.tick()
    assert await harness.state(tasks["implementation"].id) is TaskStatus.PENDING

    await RequirementRepository(harness.database.sessions).set_status(
        requirement_id, RequirementStatus.APPROVED, actor="LEDGER"
    )
    await harness.tick()
    assert await harness.state(tasks["implementation"].id) is TaskStatus.READY


async def test_implementation_only_objectives_are_not_gated(harness: Harness) -> None:
    objective_id = await harness.objective("Fix the parser crash")
    tasks = await harness.tasks(objective_id)
    assert "consolidation" not in tasks
    await harness.finish(tasks["security_review"].id)
    assert await harness.state(tasks["implementation"].id) is TaskStatus.READY
    assert tasks["implementation"].requirement_id is None
