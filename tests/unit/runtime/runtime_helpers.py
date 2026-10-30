"""Shared helpers for runtime unit tests."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.resource_monitor import HostResources
from aether.storage.models import EventRecord, TaskRecord, TaskStatus

Behaviour = Callable[[TaskRecord], Awaitable[TaskResult]]


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeMonitor:
    def snapshot(self, disk_path) -> HostResources:
        return HostResources(
            cpu_percent=1.0, available_ram_bytes=16 * 1024**3, free_disk_bytes=500 * 1024**3
        )


async def succeed(_task: TaskRecord) -> TaskResult:
    return TaskResult(status=TaskResultStatus.COMPLETED, summary="ok")


class StubAgent(BaseAgent):
    agent_id = AgentId.FORGE
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self, behaviour: Behaviour = succeed) -> None:
        super().__init__()
        self.behaviour = behaviour
        self.handled: list[AgentMessage] = []
        self.started_tasks: list[UUID] = []
        self.active = 0
        self.max_active = 0

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        self.handled.append(message)

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        self.started_tasks.append(task.id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return await self.behaviour(task)
        finally:
            self.active -= 1


def stub_for(agent_id: AgentId, behaviour: Behaviour = succeed) -> StubAgent:
    subclass = type(f"Stub{agent_id.value}", (StubAgent,), {"agent_id": agent_id})
    return subclass(behaviour)


async def wait_until(predicate: Callable[[], Awaitable[bool]], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def make_task(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: UUID,
    *,
    state: TaskStatus = TaskStatus.READY,
    agent: str | None = "FORGE",
    max_attempts: int = 3,
    attempt: int = 0,
    priority: int = 50,
) -> UUID:
    async with sessions() as session, session.begin():
        task = TaskRecord(
            objective_id=objective_id,
            title="task",
            state=state.value,
            assigned_agent=agent,
            max_attempts=max_attempts,
            attempt=attempt,
            priority=priority,
        )
        session.add(task)
        await session.flush()
        return task.id


async def get_task(sessions: async_sessionmaker[AsyncSession], task_id: UUID) -> TaskRecord:
    async with sessions() as session:
        task = await session.get(TaskRecord, task_id, populate_existing=True)
    assert task is not None
    return task


async def event_counts(
    sessions: async_sessionmaker[AsyncSession], aggregate_id: UUID | None = None
) -> Counter[str]:
    async with sessions() as session:
        statement = select(EventRecord)
        if aggregate_id is not None:
            statement = statement.where(EventRecord.aggregate_id == aggregate_id)
        return Counter(event.event_type for event in await session.scalars(statement))
