"""The requirements ledger and the operational-visibility queries."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from aether.agents.base import TaskResultStatus
from aether.communication.contracts import (
    EvaluationOutcome,
    RequirementEvaluatedPayload,
    RequirementSubmitPayload,
    RequirementType,
)
from aether.communication.messages import AgentId
from aether.storage.models import (
    EventRecord,
    RequirementRecord,
    RequirementStatus,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import (
    AuthorizationRequestRepository,
    ErrorRepository,
    ObjectiveRepository,
    TaskRepository,
)


async def requirement_rows(database):
    async with database.sessions() as session:
        return list(await session.scalars(select(RequirementRecord)))


async def test_submitted_requirement_is_recorded_once(agent, ctx, publish, database):
    objective_id = await ObjectiveRepository(database.sessions).create(title="Widgets")
    payload = RequirementSubmitPayload(
        objective_id=objective_id,
        title="Parse widget files",
        requirement_type=RequirementType.CAPABILITY,
        acceptance_criteria=["AC1: parses a file"],
    )
    message = await publish(AgentId.SCOUT, payload, objective_id=objective_id)

    await agent.handle_message(message, ctx)
    await agent.handle_message(message, ctx)
    duplicate = await publish(AgentId.ROGUE, payload, objective_id=objective_id)
    await agent.handle_message(duplicate, ctx)

    (requirement,) = await requirement_rows(database)
    assert requirement.status == RequirementStatus.PROPOSED
    assert requirement.created_by == "SCOUT"
    assert requirement.acceptance_criteria == ["AC1: parses a file"]
    async with database.sessions() as session:
        events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "requirement.created")
            )
        )
    assert len(events) == 1
    awaiting = await agent.queries(ctx).requirements_awaiting_evaluation()
    assert [item.id for item in awaiting] == [requirement.id]


async def test_evaluation_moves_requirement_through_the_ledger(agent, ctx, publish, database):
    objective_id = await ObjectiveRepository(database.sessions).create(title="Widgets")
    requirement_id = uuid4()
    submit = RequirementSubmitPayload(
        requirement_id=requirement_id,
        objective_id=objective_id,
        title="Parse widget files",
        requirement_type=RequirementType.CAPABILITY,
    )
    await agent.handle_message(await publish(AgentId.SCOUT, submit, objective_id=objective_id), ctx)

    approved = RequirementEvaluatedPayload(
        requirement_id=requirement_id,
        objective_id=objective_id,
        title="Parse widget files",
        requirement_type=RequirementType.CAPABILITY,
        acceptance_criteria=["AC1: parses a file"],
        outcome=EvaluationOutcome.APPROVED,
        rationale="worth it",
    )
    await agent.handle_message(
        await publish(AgentId.SCOUT, approved, objective_id=objective_id), ctx
    )

    (requirement,) = await requirement_rows(database)
    assert requirement.id == requirement_id
    assert requirement.status == RequirementStatus.APPROVED
    assert requirement.acceptance_criteria == ["AC1: parses a file"]
    eligible = await agent.queries(ctx).eligible_requirements()
    assert [item.id for item in eligible] == [requirement_id]

    deferred = approved.model_copy(update={"outcome": EvaluationOutcome.DEFERRED})
    await agent.handle_message(
        await publish(AgentId.SCOUT, deferred, objective_id=objective_id), ctx
    )
    rejected = approved.model_copy(update={"outcome": EvaluationOutcome.REJECTED})
    await agent.handle_message(
        await publish(AgentId.SCOUT, rejected, objective_id=objective_id), ctx
    )
    (requirement,) = await requirement_rows(database)
    assert requirement.status == RequirementStatus.REJECTED


async def test_evaluation_of_unknown_requirement_creates_it(agent, ctx, publish, database):
    objective_id = await ObjectiveRepository(database.sessions).create(title="Widgets")
    payload = RequirementEvaluatedPayload(
        requirement_id=uuid4(),
        objective_id=objective_id,
        title="Index widgets",
        requirement_type=RequirementType.IMPLEMENTATION,
        acceptance_criteria=["AC1: indexes"],
        outcome=EvaluationOutcome.APPROVED,
        rationale="needed",
    )
    await agent.handle_message(await publish(AgentId.SCOUT, payload), ctx)

    (requirement,) = await requirement_rows(database)
    assert requirement.id == payload.requirement_id
    assert requirement.status == RequirementStatus.APPROVED


async def test_operational_queries_and_snapshot(agent, ctx, database, make_work, clock):
    work = await make_work()
    tasks = TaskRepository(database.sessions)
    first = await tasks.create(
        objective_id=work.objective_id, title="first", assigned_agent="SCOUT"
    )
    second = await tasks.create(
        objective_id=work.objective_id, title="second", assigned_agent="SCOUT"
    )
    async with database.sessions() as session, session.begin():
        session.add(TaskDependencyRecord(task_id=second, depends_on_task_id=first))
    await tasks.transition(first, TaskStatus.BLOCKED, actor="ORCHESTR")
    await ErrorRepository(database.sessions).record(
        error_type="execution_failed", message="boom", agent="FORGE"
    )
    authorizations = AuthorizationRequestRepository(database.sessions)
    await authorizations.create(
        question="Allow?", actor="WARDEN", expires_at=clock() + timedelta(minutes=5)
    )
    await authorizations.create(
        question="Old?", actor="WARDEN", expires_at=clock() - timedelta(minutes=5)
    )
    queries = agent.queries(ctx)

    assert [view.id for view in await queries.blocked_tasks()] == [first]
    ready = {view.id for view in await queries.ready_tasks()}
    assert second not in ready and work.task_id in ready
    assignments = await queries.work_assignments()
    assert {view.title for view in assignments["SCOUT"]} == {"first"}  # pending is not active
    assert len(await queries.pending_authorizations()) == 1
    assert len(await queries.unresolved_errors(error_types=["execution_failed"])) == 1

    snapshot = await queries.snapshot()
    assert snapshot.blocked_tasks == 1
    assert snapshot.capability_states == {"installed": 1}
    assert snapshot.eligible_requirements == 1
    assert snapshot.pending_authorizations == 1
    assert snapshot.unresolved_errors == 1
    assert snapshot.agent_workload["SCOUT"] == {"blocked": 1, "pending": 1}


async def test_execute_task_reports_snapshot(agent, ctx, database, make_work):
    work = await make_work()
    async with database.sessions() as session:
        task = await session.get(TaskRecord, work.task_id)

    result = await agent.execute_task(task, ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert result.outputs["snapshot"]["capability_states"] == {"installed": 1}


async def test_failed_execution_is_recorded_once(agent, ctx, publish, database, make_work):
    from aether.communication.contracts import ExecutionResultPayload
    from aether.storage.models import ErrorRecord, RunStatus

    work = await make_work()
    message = await publish(
        AgentId.FORGE,
        ExecutionResultPayload(task_id=work.task_id, status=RunStatus.FAILED, error="build broke"),
        task_id=work.task_id,
    )
    await agent.handle_message(message, ctx)
    await agent.handle_message(message, ctx)

    async with database.sessions() as session:
        errors = list(await session.scalars(select(ErrorRecord)))
    assert [(error.error_type, error.aggregate_id) for error in errors] == [
        ("execution_failed", work.task_id)
    ]
