"""Shared helpers for the FORGE tests (a uniquely named module; the test tree has no packages)."""

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from aether.agents.base import AgentContext
from aether.agents.forge import ForgeAgent
from aether.agents.forge.planning import scope_plan
from aether.agents.forge.preflight import APPROVAL_KINDS, tool_call_context
from aether.agents.forge.schemas import ForgeConfig
from aether.agents.forge.store import latest_plan
from aether.communication.contracts import (
    ExecutionRequestPayload,
    PlanStep,
    ResourceEstimate,
    build_message,
)
from aether.communication.messages import AgentId, MessageType
from aether.models.contracts import CompletionRequest
from aether.runtime.leases import LeaseManager
from aether.security import ApprovalService, DecisionOutcome
from aether.storage.database import Database
from aether.storage.models import MessageRecord, RequirementStatus, TaskRecord, TaskStatus
from aether.storage.repositories import (
    ObjectiveRepository,
    RequirementRepository,
    TaskRepository,
)

CRITERIA = ["The module prints the expected greeting"]
HELLO = 'print("hello from forge")\n'


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def step(tool: str, purpose: str = "test step", **arguments: Any) -> PlanStep:
    return PlanStep(tool=tool, arguments=arguments, purpose=purpose)


def py(code: str, purpose: str = "run python") -> PlanStep:
    return step("run_process", purpose, argv=[sys.executable, "-c", code])


def write(path: str, content: str = HELLO) -> PlanStep:
    return step("write_file", f"write {path}", path=path, content=content)


@dataclass
class ForgeEnv:
    database: Database
    ctx: AgentContext
    agent: ForgeAgent
    clock: FakeClock
    approvals: ApprovalService
    roots: dict[str, Path]
    sleeps: list[float]
    llm_requests: list[CompletionRequest]
    llm_replies: list[str | Exception] = field(default_factory=list)
    config: ForgeConfig = field(default_factory=ForgeConfig)

    async def make_task(
        self,
        *,
        description: str = "",
        criteria: Sequence[str] | None = CRITERIA,
        with_requirement: bool = True,
        requirement_status: RequirementStatus = RequirementStatus.APPROVED,
        requirement_description: str = "",
        assigned_agent: str | None = "FORGE",
        running: bool = True,
        max_attempts: int = 3,
        lease_ttl: timedelta = timedelta(minutes=10),
    ) -> TaskRecord:
        sessions = self.database.sessions
        objective_id = await ObjectiveRepository(sessions).create(title="Build a feature")
        requirement_id: UUID | None = None
        if with_requirement:
            requirements = RequirementRepository(sessions)
            requirement_id = await requirements.create(
                objective_id=objective_id,
                title="Greeting module",
                requirement_type="capability",
                created_by="SCOUT",
                description=requirement_description,
                acceptance_criteria=list(criteria or []),
            )
            if requirement_status is not RequirementStatus.PROPOSED:
                await requirements.set_status(requirement_id, requirement_status, actor="LEDGER")
        task_id = await TaskRepository(sessions).create(
            objective_id=objective_id,
            title="Implement the greeting module",
            description=description,
            assigned_agent=assigned_agent,
            requirement_id=requirement_id,
            max_attempts=max_attempts,
        )
        if running:
            await self.start(task_id, lease_ttl)
        return await self.task(task_id)

    async def start(self, task_id: UUID, ttl: timedelta = timedelta(minutes=10)) -> None:
        sessions = self.database.sessions
        await TaskRepository(sessions).transition(task_id, TaskStatus.READY, actor="test")
        lease = await LeaseManager(sessions, clock=self.clock).acquire(task_id, "worker-1", ttl)
        assert lease is not None

    async def task(self, task_id: UUID) -> TaskRecord:
        async with self.database.sessions() as session:
            record = await session.get(TaskRecord, task_id, populate_existing=True)
        assert record is not None
        return record

    async def deliver_plan(
        self,
        task: TaskRecord,
        steps: Sequence[PlanStep],
        rollback: Sequence[PlanStep] = (),
        *,
        criteria: Sequence[str] = CRITERIA,
        estimate: ResourceEstimate | None = None,
        depends_on: Sequence[UUID] = (),
        plan_id: UUID | None = None,
    ) -> UUID:
        plan_id = plan_id or uuid4()
        payload = ExecutionRequestPayload(
            task_id=task.id,
            objective_id=task.objective_id,
            requirement_id=task.requirement_id,
            plan_id=plan_id,
            steps=list(steps),
            rollback_steps=list(rollback),
            acceptance_criteria=list(criteria),
            depends_on=list(depends_on),
            resource_estimate=estimate or ResourceEstimate(ram_mb=128, disk_mb=16, duration_s=60),
        )
        message = build_message(
            AgentId.ORCHESTR,
            AgentId.FORGE,
            payload,
            task_id=task.id,
            objective_id=task.objective_id,
            requirement_id=task.requirement_id,
        )
        assert await self.ctx.bus.publish(message)
        return plan_id

    async def approve_plan(
        self, task: TaskRecord, *, decision: DecisionOutcome = DecisionOutcome.APPROVED
    ) -> list[str]:
        """WARDEN-decide every approval-requiring operation of the task's scoped plan."""
        plan = await latest_plan(self.ctx, task.id)
        assert plan is not None
        scoped = scope_plan(plan, self.config)
        assert self.ctx.tools is not None
        kinds = {info.name: info.kind for info in self.ctx.tools.list_tools()}
        hashes = []
        for item in (*scoped.steps, *scoped.rollback_steps):
            if kinds[item.tool] not in APPROVAL_KINDS:
                continue
            operation = self.ctx.tools.build_operation(
                item.tool, item.arguments, tool_call_context(task)
            )
            await self.approvals.record_decision(
                AgentId.WARDEN,
                operation,
                decision,
                rationale="Reviewed under test policy",
                evidence=[{"check": "forge-test"}],
            )
            hashes.append(operation.input_hash)
        return hashes

    async def messages(
        self,
        message_type: MessageType | None = None,
        *,
        source: AgentId = AgentId.FORGE,
        task_id: UUID | None = None,
    ) -> list[MessageRecord]:
        statement = select(MessageRecord).where(MessageRecord.source_agent == source.value)
        if message_type is not None:
            statement = statement.where(MessageRecord.message_type == message_type.value)
        if task_id is not None:
            statement = statement.where(MessageRecord.task_id == task_id)
        async with self.database.sessions() as session:
            return list(await session.scalars(statement.order_by(MessageRecord.created_at)))

    async def journal(self, task_id: UUID) -> list[dict[str, Any]]:
        records = await self.messages(MessageType.PROGRESS_UPDATE, task_id=task_id)
        entries = [record.payload["details"] for record in records]
        entries = [item for item in entries if item.get("forge_journal")]
        return sorted(entries, key=lambda item: item["seq"])

    def workspace(self, task: TaskRecord) -> Path:
        return self.roots["workspace"] / str(task.id)
