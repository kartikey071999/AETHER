import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.agents.ledger import LedgerAgent
from aether.communication.bus import MessageBus
from aether.communication.contracts import Payload, SecurityDecisionPayload, build_message
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.observability.recorder import EventRecorder
from aether.observability.spool import EventSpool
from aether.security import (
    ApprovalService,
    DecisionOutcome,
    Operation,
    PermissionService,
    SecurityPolicy,
)
from aether.storage.database import Database
from aether.storage.models import CapabilityStatus, MessageRecord, RequirementStatus
from aether.storage.repositories import (
    CapabilityRepository,
    ObjectiveRepository,
    RequirementRepository,
    TaskRepository,
)
from aether.tools import AgentTools
from aether.tools.adapters import build_default_registry

CRITERIA = ["AC1: adds two numbers", "AC2: rejects text input"]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = schema_database(tmp_path / "ledger.db")
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
    return SecurityPolicy(
        workspace_dir=workspace,
        artifact_dir=tmp_path / "artifacts",
        data_dir=tmp_path / "data",
        allowed_executables=[sys.executable],
        allowed_hosts=["api.example.com"],
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
def ctx(
    database: Database,
    recorder: EventRecorder,
    policy: SecurityPolicy,
    clock: FakeClock,
    approvals: ApprovalService,
) -> AgentContext:
    permissions = PermissionService(database.sessions, recorder, policy, clock=clock)
    registry = build_default_registry(permissions, database.sessions, recorder)
    return AgentContext(
        sessions=database.sessions,
        bus=MessageBus(database.sessions),
        clock=clock,
        tools=AgentTools(AgentId.LEDGER, registry),
        services={"approvals": approvals},
    )


@pytest.fixture
def agent() -> LedgerAgent:
    return LedgerAgent()


@dataclass
class Work:
    """An objective, an approved requirement, FORGE's task and an installed capability."""

    objective_id: UUID
    requirement_id: UUID
    task_id: UUID
    capability_id: UUID
    criteria: list[str] = field(default_factory=lambda: list(CRITERIA))


async def _install_capability(
    database: Database, name: str, objective_id: UUID, requirement_id: UUID
) -> UUID:
    capabilities = CapabilityRepository(database.sessions)
    capability_id = await capabilities.register(
        name=name, actor="SCOUT", objective_id=objective_id, requirement_id=requirement_id
    )
    for status in (
        CapabilityStatus.PROPOSED,
        CapabilityStatus.APPROVED,
        CapabilityStatus.INSTALLED,
    ):
        await capabilities.transition(capability_id, status, actor="FORGE")
    return capability_id


@pytest.fixture
def make_work(database: Database) -> Callable[..., Awaitable[Work]]:
    async def make(name: str = "widget") -> Work:
        objective_id = await ObjectiveRepository(database.sessions).create(title="Build widgets")
        requirements = RequirementRepository(database.sessions)
        requirement_id = await requirements.create(
            objective_id=objective_id,
            title=f"{name} capability",
            requirement_type="capability",
            created_by="SCOUT",
            acceptance_criteria=list(CRITERIA),
        )
        await requirements.set_status(requirement_id, RequirementStatus.APPROVED, actor="SCOUT")
        task_id = await TaskRepository(database.sessions).create(
            objective_id=objective_id,
            title=f"Implement {name}",
            assigned_agent="FORGE",
            requirement_id=requirement_id,
        )
        capability_id = await _install_capability(database, name, objective_id, requirement_id)
        return Work(objective_id, requirement_id, task_id, capability_id)

    return make


Publish = Callable[..., Awaitable[AgentMessage]]


@pytest.fixture
def publish(ctx: AgentContext) -> Publish:
    """Publish a validated message (as the real sender would) so LEDGER can reload it."""

    async def send(source: AgentId, payload: Payload, **envelope: Any) -> AgentMessage:
        message = build_message(source, AgentId.LEDGER, payload, **envelope)
        await ctx.bus.publish(message)
        return message

    return send


@pytest.fixture
def sent(database: Database) -> Callable[..., Awaitable[list[MessageRecord]]]:
    async def load(
        message_type: MessageType, *, source: AgentId = AgentId.LEDGER
    ) -> list[MessageRecord]:
        statement = select(MessageRecord).where(
            MessageRecord.message_type == message_type.value,
            MessageRecord.source_agent == source.value,
        )
        async with database.sessions() as session:
            return list(await session.scalars(statement.order_by(MessageRecord.timestamp)))

    return load


@pytest.fixture
def approve_pending(
    approvals: ApprovalService, database: Database
) -> Callable[..., Awaitable[list[AgentMessage]]]:
    """Play WARDEN: decide every operation LEDGER asked to review and build the decisions."""

    async def approve(
        outcome: DecisionOutcome = DecisionOutcome.APPROVED,
    ) -> list[AgentMessage]:
        statement = select(MessageRecord).where(
            MessageRecord.message_type == MessageType.SECURITY_EVALUATE.value
        )
        async with database.sessions() as session:
            rows = list(await session.scalars(statement))
        decisions: list[AgentMessage] = []
        seen: set[str] = set()
        for row in rows:
            operation = Operation.model_validate(row.payload["operation"])
            if operation.input_hash in seen:
                continue
            seen.add(operation.input_hash)
            decision = await approvals.record_decision(
                AgentId.WARDEN,
                operation,
                outcome,
                rationale="reviewed acceptance suite run",
                evidence=[{"check": "unit-test"}],
            )
            payload = SecurityDecisionPayload(
                decision_id=decision.decision_id,
                decision=decision.decision,
                input_hash=decision.input_hash,
                rationale=decision.rationale,
            )
            decisions.append(build_message(AgentId.WARDEN, AgentId.LEDGER, payload))
        return decisions

    return approve
