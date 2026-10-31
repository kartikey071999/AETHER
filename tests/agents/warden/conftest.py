import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.agents.warden import WardenAgent
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.models.facade import AgentModels
from aether.models.providers import MockProvider
from aether.observability.recorder import EventRecorder
from aether.observability.spool import EventSpool
from aether.security import (
    ApprovalService,
    Operation,
    OperationKind,
    PermissionService,
    SecurityPolicy,
)
from aether.security.policy import default_agent_permissions
from aether.storage.database import Database
from aether.storage.models import (
    MessageRecord,
    RequirementStatus,
    SecurityDecisionRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import ObjectiveRepository, RequirementRepository, TaskRepository


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = schema_database(tmp_path / "warden.db")
    yield database
    await database.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def policy(tmp_path: Path, workspace: Path) -> SecurityPolicy:
    for name in ("artifacts", "data"):
        (tmp_path / name).mkdir()
    permissions = dict(default_agent_permissions())
    # Give FORGE a high-impact kind so the user-authorization path can be exercised.
    permissions[AgentId.FORGE] = permissions[AgentId.FORGE] | {OperationKind.CONFIG_CHANGE}
    return SecurityPolicy(
        workspace_dir=workspace,
        artifact_dir=tmp_path / "artifacts",
        data_dir=tmp_path / "data",
        allowed_executables=[sys.executable],
        allowed_hosts=["pypi.org", "github.com", "api.example.com"],
        agent_permissions=permissions,
        max_approval_ttl=timedelta(hours=1),
        authorization_ttl=timedelta(minutes=5),
        subprocess_timeout_seconds=120,
        max_output_bytes=65536,
    )


@pytest.fixture
def recorder(tmp_path: Path) -> EventRecorder:
    return EventRecorder(EventSpool(tmp_path / "spool" / "events.jsonl"))


@pytest.fixture
def approvals(
    database: Database, recorder: EventRecorder, policy: SecurityPolicy, clock: FakeClock
) -> ApprovalService:
    return ApprovalService(database.sessions, recorder, policy, clock=clock)


@pytest.fixture
def permissions(
    database: Database, recorder: EventRecorder, policy: SecurityPolicy, clock: FakeClock
) -> PermissionService:
    """The enforcement side: what the requester's tool call would see."""
    return PermissionService(database.sessions, recorder, policy, clock=clock)


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def ctx(
    database: Database, clock: FakeClock, approvals: ApprovalService, provider: MockProvider
) -> AgentContext:
    return AgentContext(
        sessions=database.sessions,
        bus=MessageBus(database.sessions),
        clock=clock,
        models=AgentModels(AgentId.WARDEN, provider.complete),
        services={"approvals": approvals},
    )


@pytest.fixture
def with_model() -> Callable[[AgentContext, list[Any]], tuple[AgentContext, MockProvider]]:
    """A copy of ``ctx`` whose model returns the scripted responses."""

    def build(ctx: AgentContext, responses: list[Any]) -> tuple[AgentContext, MockProvider]:
        scripted = MockProvider(responses)
        return replace(ctx, models=AgentModels(AgentId.WARDEN, scripted.complete)), scripted

    return build


@pytest.fixture
def agent() -> WardenAgent:
    return WardenAgent()


@dataclass
class Work:
    """An objective, an approved requirement and FORGE's task (left PENDING)."""

    objective_id: UUID
    requirement_id: UUID
    task_id: UUID


@pytest.fixture
def make_work(database: Database) -> Callable[..., Awaitable[Work]]:
    async def make(
        *,
        title: str = "Install the requests HTTP library",
        status: RequirementStatus = RequirementStatus.APPROVED,
        agent: str = "FORGE",
    ) -> Work:
        objective_id = await ObjectiveRepository(database.sessions).create(title="Fetch data")
        requirements = RequirementRepository(database.sessions)
        requirement_id = await requirements.create(
            objective_id=objective_id,
            title=title,
            description="Use the requests package to call the weather API.",
            requirement_type="dependency",
            created_by="SCOUT",
            acceptance_criteria=["AC1: requests is importable"],
        )
        if status is not RequirementStatus.PROPOSED:
            await requirements.set_status(requirement_id, status, actor="SCOUT")
        task_id = await TaskRepository(database.sessions).create(
            objective_id=objective_id,
            title="Install dependency",
            assigned_agent=agent,
            requirement_id=requirement_id,
        )
        return Work(objective_id, requirement_id, task_id)

    return make


@pytest.fixture
def move_task(database: Database) -> Callable[..., Awaitable[None]]:
    """Walk a task through the state machine (as the scheduler/worker would)."""

    async def move(task_id: UUID, *states: TaskStatus) -> None:
        tasks = TaskRepository(database.sessions)
        for state in states:
            await tasks.transition(task_id, state, actor="ORCHESTR")

    return move


RUN_TO_WAIT = (
    TaskStatus.READY,
    TaskStatus.SCHEDULED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_FOR_APPROVAL,
)


@pytest.fixture
def to_waiting(move_task: Callable[..., Awaitable[None]]) -> Callable[[UUID], Awaitable[None]]:
    """Move a fresh task to ``waiting_for_approval`` (its tool call was routed to WARDEN)."""

    async def move(task_id: UUID) -> None:
        await move_task(task_id, *RUN_TO_WAIT)

    return move


@pytest.fixture
def task_state(database: Database) -> Callable[[UUID], Awaitable[str]]:
    async def load(task_id: UUID) -> str:
        async with database.sessions() as session:
            task = await session.get(TaskRecord, task_id)
            assert task is not None
            return task.state

    return load


@pytest.fixture
def evaluate_message() -> Callable[..., AgentMessage]:
    """A security.evaluate message as ``request_security_review`` would send it."""

    def build(
        operation: Operation,
        *,
        evidence: list[dict[str, Any]] | None = None,
        input_hash: str | None = None,
        source: AgentId | None = None,
    ) -> AgentMessage:
        payload: dict[str, Any] = {
            "operation": operation.model_dump(mode="json"),
            "input_hash": input_hash or operation.input_hash,
            "block_reason": "approval_missing",
        }
        if evidence is not None:
            payload["evidence"] = evidence
        return AgentMessage(
            source_agent=source or operation.requested_by,
            target_agent=AgentId.WARDEN,
            message_type=MessageType.SECURITY_EVALUATE,
            objective_id=operation.objective_id,
            task_id=operation.task_id,
            requirement_id=operation.declared_requirement_id,
            payload=payload,
            requires_response=True,
        )

    return build


@pytest.fixture
def decisions(database: Database) -> Callable[..., Awaitable[list[SecurityDecisionRecord]]]:
    async def load(input_hash: str | None = None) -> list[SecurityDecisionRecord]:
        statement = select(SecurityDecisionRecord)
        if input_hash is not None:
            statement = statement.where(SecurityDecisionRecord.input_hash == input_hash)
        async with database.sessions() as session:
            return list(
                await session.scalars(statement.order_by(SecurityDecisionRecord.created_at))
            )

    return load


@pytest.fixture
def sent(database: Database) -> Callable[..., Awaitable[list[MessageRecord]]]:
    async def load(message_type: MessageType) -> list[MessageRecord]:
        statement = select(MessageRecord).where(
            MessageRecord.message_type == message_type.value,
            MessageRecord.source_agent == AgentId.WARDEN.value,
        )
        async with database.sessions() as session:
            return list(await session.scalars(statement.order_by(MessageRecord.timestamp)))

    return load
