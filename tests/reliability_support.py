"""Shared helpers for the recovery/reliability suite (tests/recovery, tests/scheduling).

Everything here is offline: SQLite files under ``tmp_path`` by default, and PostgreSQL only when
``AETHER_TEST_POSTGRES_URL`` is set, inside a throw-away schema that is dropped afterwards.
"""

import asyncio
import os
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.resource_monitor import HostResources
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.storage.database import Database
from aether.storage.models import Base, EventRecord, MessageRecord, TaskRecord, TaskStatus

POSTGRES_URL_ENV = "AETHER_TEST_POSTGRES_URL"

Behaviour = Callable[[TaskRecord], Awaitable[TaskResult]]


# -- clocks, monitors, agents ----------------------------------------------------------------


class FakeClock:
    """A settable clock; starts at the real current time so wall-clock waits still line up."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class QuietMonitor:
    """Plenty of free resources, so the scheduler never defers for host pressure."""

    def snapshot(self, disk_path: Path) -> HostResources:
        return HostResources(
            cpu_percent=1.0, available_ram_bytes=16 * 1024**3, free_disk_bytes=500 * 1024**3
        )


async def succeed(_task: TaskRecord) -> TaskResult:
    return TaskResult(status=TaskResultStatus.COMPLETED, summary="ok")


class RecordingAgent(BaseAgent):
    """FORGE stand-in recording every task execution and message it handles."""

    agent_id = AgentId.FORGE
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self, behaviour: Behaviour = succeed) -> None:
        super().__init__()
        self.behaviour = behaviour
        self.executed: list[UUID] = []
        self.handled: list[UUID] = []

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        self.handled.append(message.message_id)

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        self.executed.append(task.id)
        return await self.behaviour(task)


def engine_limits(**overrides: Any) -> EngineLimits:
    options: dict[str, Any] = {
        "max_concurrent_tasks": 2,
        "scheduler_interval_seconds": 0.02,
        "message_poll_seconds": 0.02,
        "tick_poll_seconds": 0.02,
        "recovery_interval_seconds": 0.05,
        "lease_ttl_seconds": 30.0,
        "lease_renew_seconds": 0.05,
        "task_control_poll_seconds": 0.02,
        "task_interrupt_grace_seconds": 0.05,
        "shutdown_timeout_seconds": 0.2,
    }
    options.update(overrides)
    return EngineLimits(**options)


def build_engine(
    sessions: async_sessionmaker[AsyncSession],
    agents: list[BaseAgent],
    *,
    limits: EngineLimits | None = None,
    **kwargs: Any,
) -> Engine:
    return Engine(
        sessions,
        agents,
        Scheduler(SchedulerPolicy(max_concurrent_tasks=4), sessions),
        QuietMonitor(),
        limits or engine_limits(),
        **kwargs,
    )


# -- polling and record helpers --------------------------------------------------------------


async def wait_until(predicate: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
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
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    title: str = "task",
) -> UUID:
    async with sessions() as session, session.begin():
        task = TaskRecord(
            objective_id=objective_id,
            title=title,
            state=state.value,
            assigned_agent=agent,
            max_attempts=max_attempts,
            attempt=attempt,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        )
        session.add(task)
        await session.flush()
        return task.id


async def get_task(sessions: async_sessionmaker[AsyncSession], task_id: UUID) -> TaskRecord:
    async with sessions() as session:
        task = await session.get(TaskRecord, task_id, populate_existing=True)
    assert task is not None
    return task


async def task_state(sessions: async_sessionmaker[AsyncSession], task_id: UUID) -> TaskStatus:
    return TaskStatus((await get_task(sessions, task_id)).state)


async def event_counts(
    sessions: async_sessionmaker[AsyncSession], aggregate_id: UUID | None = None
) -> Counter[str]:
    async with sessions() as session:
        statement = select(EventRecord.event_type)
        if aggregate_id is not None:
            statement = statement.where(EventRecord.aggregate_id == aggregate_id)
        return Counter(await session.scalars(statement))


async def events_of(
    sessions: async_sessionmaker[AsyncSession], event_type: str, aggregate_id: UUID | None = None
) -> list[EventRecord]:
    async with sessions() as session:
        statement = select(EventRecord).where(EventRecord.event_type == event_type)
        if aggregate_id is not None:
            statement = statement.where(EventRecord.aggregate_id == aggregate_id)
        return list(await session.scalars(statement.order_by(EventRecord.created_at)))


async def message_status(sessions: async_sessionmaker[AsyncSession], message_id: UUID) -> str:
    async with sessions() as session:
        status = await session.scalar(
            select(MessageRecord.status).where(MessageRecord.id == message_id)
        )
    assert status is not None
    return status


def assign_message(target: AgentId = AgentId.FORGE, **overrides: Any) -> AgentMessage:
    values: dict[str, Any] = {
        "source_agent": AgentId.ORCHESTR,
        "target_agent": target,
        "message_type": MessageType.TASK_ASSIGN,
        "payload": {},
    }
    values.update(overrides)
    return AgentMessage(**values)


# -- failure injection -----------------------------------------------------------------------


_FLUSHED_EVENT_TYPES = "aether_test_flushed_event_types"


def _remember_flushed_events(session: Session, *_args: object) -> None:
    # Repositories may flush mid-transaction; the identity map only holds weak references, so
    # record staged event types at every flush instead of scanning the session at commit time.
    types = session.info.setdefault(_FLUSHED_EVENT_TYPES, set())
    types.update(item.event_type for item in session.new if isinstance(item, EventRecord))


def _forget_flushed_events(session: Session, *_args: object) -> None:
    session.info.pop(_FLUSHED_EVENT_TYPES, None)


def _staged_event_types(session: Session) -> set[str]:
    objects = [*session.new, *session.identity_map.values()]
    staged = {item.event_type for item in objects if isinstance(item, EventRecord)}
    return staged | session.info.get(_FLUSHED_EVENT_TYPES, set())


class FailingCommits:
    """A session factory whose COMMIT fails, while armed, after every statement was sent.

    The failure is raised from ``before_commit`` after an explicit flush, so the database has
    seen every INSERT/UPDATE of the transaction and only the commit itself is lost (as when the
    connection drops at commit). ``fail_on`` limits the failure to transactions staging a given
    event type, so background loops sharing the factory are unaffected.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self.session_class: type[Session] = type("FlakySession", (Session,), {})
        self.sessions = async_sessionmaker(
            engine,
            class_=AsyncSession,
            sync_session_class=self.session_class,
            expire_on_commit=False,
        )
        self.failures = 0
        self._remaining = 0
        self._event_type: str | None = None
        event.listen(self.session_class, "before_flush", _remember_flushed_events)
        event.listen(self.session_class, "after_commit", _forget_flushed_events)
        event.listen(self.session_class, "after_rollback", _forget_flushed_events)
        event.listen(self.session_class, "before_commit", self._before_commit)

    def fail_next(self, count: int = 1, *, on_event: str | None = None) -> None:
        self._remaining = count
        self._event_type = on_event

    def heal(self) -> None:
        self._remaining = 0

    def _before_commit(self, session: Session) -> None:
        if self._remaining <= 0:
            return
        staged = _staged_event_types(session)  # before the flush: the identity map is weak
        session.flush()
        if self._event_type is not None and self._event_type not in staged:
            return
        self._remaining -= 1
        self.failures += 1
        raise OperationalError(
            "COMMIT", {}, ConnectionResetError("database connection lost during commit")
        )


# -- databases -------------------------------------------------------------------------------


async def create_schema(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def sqlite_database(path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    try:
        await create_schema(database)
        yield database
    finally:
        await database.close()


class SchemaDatabase(Database):
    """A ``Database`` whose connections use one PostgreSQL schema as their search path."""

    def __init__(self, url: str, schema: str) -> None:
        driver = make_url(url).drivername
        if driver.endswith("+asyncpg"):
            connect_args: dict[str, Any] = {"server_settings": {"search_path": schema}}
        elif driver.endswith("+psycopg"):
            connect_args = {"options": f"-csearch_path={schema}"}
        else:
            raise ValueError(f"unsupported PostgreSQL driver for tests: {driver}")
        self.engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self.sessions = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)


def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is not set")
    if not make_url(url).drivername.startswith("postgresql"):
        pytest.skip(f"{POSTGRES_URL_ENV} must point at PostgreSQL")
    return url


@asynccontextmanager
async def postgres_schema() -> AsyncIterator[tuple[str, str]]:
    """A fresh, uniquely named schema (``(url, schema)``) dropped with CASCADE afterwards.

    Only objects inside this test-created schema are ever created or dropped.
    """
    url = postgres_url()
    schema = f"aether_test_{uuid4().hex[:12]}"
    admin = create_async_engine(url)
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            yield url, schema
        finally:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await admin.dispose()


@asynccontextmanager
async def postgres_databases(count: int = 1) -> AsyncIterator[list[Database]]:
    """``count`` independent connection pools (one per simulated process) on one schema."""
    async with postgres_schema() as (url, schema):
        databases: list[Database] = [SchemaDatabase(url, schema) for _ in range(count)]
        try:
            await create_schema(databases[0])
            yield databases
        finally:
            for database in databases:
                await database.close()
