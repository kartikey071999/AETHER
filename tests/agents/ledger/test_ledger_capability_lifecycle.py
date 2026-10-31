"""LEDGER advances the capability lifecycle FORGE and SCOUT report (T-040 regressions)."""

from uuid import UUID, uuid4

from aether.agents.ledger import TestRunStore
from aether.communication.contracts import (
    EvaluationOutcome,
    ExecutionResultPayload,
    RequirementEvaluatedPayload,
    RequirementType,
    VerificationRequestPayload,
)
from aether.communication.messages import AgentId, MessageType
from aether.storage.database import Database
from aether.storage.models import CapabilityRecord, CapabilityStatus, RunStatus
from aether.storage.repositories import (
    CapabilityRepository,
    ObjectiveRepository,
    RequirementRepository,
    TaskRepository,
)

CRITERIA = ["AC1: adds two numbers", "AC2: rejects text input"]


async def _proposed(database: Database) -> tuple[UUID, UUID, UUID]:
    """An objective, SCOUT's proposed requirement and the capability proposed to satisfy it."""
    objective_id = await ObjectiveRepository(database.sessions).create(title="Widgets")
    requirement_id = await RequirementRepository(database.sessions).create(
        objective_id=objective_id,
        title="Evaluate widget parser",
        requirement_type="capability",
        created_by="SCOUT",
        acceptance_criteria=list(CRITERIA),
    )
    capabilities = CapabilityRepository(database.sessions)
    capability_id = await capabilities.register(
        name="widget-parser",
        actor="SCOUT",
        objective_id=objective_id,
        requirement_id=requirement_id,
    )
    await capabilities.transition(capability_id, CapabilityStatus.PROPOSED, actor="SCOUT")
    return objective_id, requirement_id, capability_id


async def _status(database: Database, capability_id: UUID) -> str:
    async with database.sessions() as session:
        capability = await session.get(CapabilityRecord, capability_id)
        assert capability is not None
        return capability.lifecycle_status


async def _evaluate(agent, ctx, publish, objective_id, requirement_id, outcome) -> None:
    payload = RequirementEvaluatedPayload(
        requirement_id=requirement_id,
        objective_id=objective_id,
        title="Evaluate widget parser",
        requirement_type=RequirementType.CAPABILITY,
        acceptance_criteria=list(CRITERIA),
        outcome=outcome,
        rationale="evidence-backed",
    )
    await agent.handle_message(
        await publish(AgentId.SCOUT, payload, objective_id=objective_id), ctx
    )


async def test_approving_a_requirement_approves_its_proposed_capability(
    agent, ctx, publish, database
):
    objective_id, requirement_id, capability_id = await _proposed(database)

    await _evaluate(agent, ctx, publish, objective_id, requirement_id, EvaluationOutcome.APPROVED)

    assert await _status(database, capability_id) == CapabilityStatus.APPROVED


async def test_rejected_requirement_leaves_the_capability_proposed(agent, ctx, publish, database):
    objective_id, requirement_id, capability_id = await _proposed(database)

    await _evaluate(agent, ctx, publish, objective_id, requirement_id, EvaluationOutcome.REJECTED)

    assert await _status(database, capability_id) == CapabilityStatus.PROPOSED


async def test_verification_request_installs_an_approved_capability_before_verifying(
    agent, ctx, publish, database, workspace, sent
):
    objective_id, requirement_id, capability_id = await _proposed(database)
    await _evaluate(agent, ctx, publish, objective_id, requirement_id, EvaluationOutcome.APPROVED)
    (workspace / "test_widget.py").write_text("def test_ac1():\n    pass\n", encoding="utf-8")

    message = await publish(
        AgentId.FORGE,
        VerificationRequestPayload(
            capability_id=capability_id,
            requirement_id=requirement_id,
            suite="test_widget.py",
            acceptance_criteria=list(CRITERIA),
            execution_run_ids=[uuid4()],
        ),
        objective_id=objective_id,
        requirement_id=requirement_id,
    )
    await agent.handle_message(message, ctx)

    # FORGE recorded the build, so the capability is installed (still unverified) and the
    # pytest run now waits for WARDEN.
    assert await _status(database, capability_id) == CapabilityStatus.INSTALLED
    assert len(await sent(MessageType.SECURITY_EVALUATE)) == 1


async def test_success_claim_with_requested_verification_is_not_verified_twice(
    agent, ctx, publish, database, sent
):
    objective_id, requirement_id, capability_id = await _proposed(database)
    await _evaluate(agent, ctx, publish, objective_id, requirement_id, EvaluationOutcome.APPROVED)
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id,
        title="Implement widget parser",
        assigned_agent="FORGE",
        requirement_id=requirement_id,
    )

    message = await publish(
        AgentId.FORGE,
        ExecutionResultPayload(
            task_id=task_id,
            requirement_id=requirement_id,
            status=RunStatus.SUCCEEDED,
            execution_run_ids=[uuid4()],
            outputs={"capability_id": str(capability_id), "verification_requested": True},
        ),
        objective_id=objective_id,
        task_id=task_id,
        requirement_id=requirement_id,
    )
    await agent.handle_message(message, ctx)

    # The build is recorded; the dedicated verification.request will run the suite.
    assert await _status(database, capability_id) == CapabilityStatus.INSTALLED
    assert await TestRunStore(ctx.sessions).get(message.message_id) is None
    assert await sent(MessageType.TEST_REPORT) == []
    assert await sent(MessageType.SECURITY_EVALUATE) == []
