"""Test doubles and the ORCHESTR test harness (imported by tests; fixtures live in conftest)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from aether.agents.base import AgentContext
from aether.agents.orchestr import OrchestrAgent
from aether.agents.orchestr.limits import LIMITS_SERVICE, OrchestrLimits
from aether.agents.orchestr.store import OrchestrStore
from aether.communication.contracts import (
    EvaluationOutcome,
    ObjectiveCreatePayload,
    Payload,
    RequirementEvaluatedPayload,
    RequirementType,
    TaskResultPayload,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.resource_monitor import HostResources
from aether.storage.database import Database
from aether.storage.models import ObjectiveRecord, RequirementStatus, TaskRecord, TaskStatus
from aether.storage.repositories import RequirementRepository

GIB = 1024**3
ALL_STATUSES = ["active", "deferred", "paused", "blocked", "completed", "cancelled"]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeMonitor:
    def __init__(self) -> None:
        self.free_disk = 500 * GIB
        self.ram = 16 * GIB
        self.cpu = 5.0
        self.fail = False

    def snapshot(self, disk_path: Path) -> HostResources:
        if self.fail:
            raise OSError("monitor down")
        return HostResources(
            cpu_percent=self.cpu, available_ram_bytes=self.ram, free_disk_bytes=self.free_disk
        )


class FakeBus:
    """Collects published messages; republishing a message id is a no-op like the real bus."""

    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> bool:
        if any(item.message_id == message.message_id for item in self.messages):
            return False
        self.messages.append(message)
        return True

    def of_type(self, message_type: MessageType, target: AgentId | None = None) -> list[Any]:
        return [
            item
            for item in self.messages
            if item.message_type is message_type and (target is None or item.target_agent is target)
        ]


@dataclass
class Harness:
    database: Database
    bus: FakeBus
    clock: FakeClock
    monitor: FakeMonitor
    ctx: AgentContext
    agent: OrchestrAgent
    store: OrchestrStore

    def restart(self) -> OrchestrAgent:
        """A fresh agent instance over the same database, as after a process restart."""
        self.agent = OrchestrAgent()
        return self.agent

    def use_limits(self, limits: OrchestrLimits) -> None:
        self.ctx.services[LIMITS_SERVICE] = limits

    async def send(
        self,
        source: AgentId,
        payload: Payload,
        *,
        target: AgentId = AgentId.ORCHESTR,
        **envelope: Any,
    ) -> AgentMessage:
        message = build_message(source, target, payload, **envelope)
        await self.agent.dispatch_message(message, self.ctx)
        return message

    async def objective(
        self,
        title: str = "Add a PDF parsing capability",
        *,
        source: AgentId = AgentId.MUSE,
        criteria: Iterable[str] = ("PDF text is extracted correctly",),
        **fields: Any,
    ) -> UUID:
        payload = ObjectiveCreatePayload(title=title, completion_criteria=list(criteria), **fields)
        await self.send(source, payload)
        return await self.find_objective(title)

    async def find_objective(self, title: str) -> UUID:
        objectives = await self.store.list_objectives(ALL_STATUSES)
        return next(item for item in objectives if item.title == title).id

    async def get_objective(self, objective_id: UUID) -> ObjectiveRecord:
        found = await self.store.get_objective(objective_id)
        assert found is not None
        return found

    async def tasks(self, objective_id: UUID) -> dict[str, TaskRecord]:
        records = await self.store.tasks_of(objective_id)
        return {record.idempotency_key.split(":", 2)[-1]: record for record in records}

    async def state(self, task_id: UUID) -> TaskStatus:
        task = await self.store.get_task(task_id)
        assert task is not None
        return TaskStatus(task.state)

    async def run_to(self, task_id: UUID, outcome: TaskStatus = TaskStatus.COMPLETED) -> None:
        """Play the worker pool: READY -> SCHEDULED -> RUNNING -> outcome."""
        for step in (TaskStatus.SCHEDULED, TaskStatus.RUNNING, outcome):
            await self.store.tasks.transition(task_id, step, actor="test")

    async def finish(self, task_id: UUID) -> None:
        await self.run_to(task_id)
        await self.agent.tick(self.ctx)

    async def tick(self) -> None:
        await self.agent.tick(self.ctx)

    async def propose_requirement(
        self, objective_id: UUID, *, approved: bool = False, title: str = "PDF parsing"
    ) -> UUID:
        """SCOUT's requirement record; ``approved`` mirrors LEDGER having flipped its status."""
        requirements = RequirementRepository(self.database.sessions)
        requirement_id = await requirements.create(
            objective_id=objective_id,
            title=title,
            requirement_type="capability",
            created_by="SCOUT",
            acceptance_criteria=["PDF text is extracted correctly"],
        )
        if approved:
            await requirements.set_status(
                requirement_id, RequirementStatus.APPROVED, actor="LEDGER"
            )
        return requirement_id

    async def evaluate_requirement(
        self, objective_id: UUID, requirement_id: UUID, *, title: str = "PDF parsing"
    ) -> AgentMessage:
        return await self.send(
            AgentId.SCOUT,
            RequirementEvaluatedPayload(
                requirement_id=requirement_id,
                objective_id=objective_id,
                title=title,
                requirement_type=RequirementType.CAPABILITY,
                acceptance_criteria=["PDF text is extracted correctly"],
                outcome=EvaluationOutcome.APPROVED,
                rationale="Meets the objective",
            ),
            objective_id=objective_id,
        )

    async def approve_requirement(self, objective_id: UUID) -> UUID:
        """An approved requirement, announced to ORCHESTR so it links the implementation tasks."""
        requirement_id = await self.propose_requirement(objective_id, approved=True)
        await self.evaluate_requirement(objective_id, requirement_id)
        return requirement_id

    def result_message(self, task: TaskRecord, payload: TaskResultPayload) -> AgentMessage:
        return build_message(
            AgentId(task.assigned_agent),
            AgentId.ORCHESTR,
            payload,
            task_id=task.id,
            objective_id=task.objective_id,
            message_id=uuid4(),
        )
