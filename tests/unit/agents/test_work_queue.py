"""Deterministic per-agent work queues (T-053): visibility, ordering, readiness, denials."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.agents.work_queue import (
    MAX_LIMIT,
    AgentWorkQueue,
    TaskAccessDenied,
    WorkQueueService,
    queue_order,
    work_queue,
)
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.runtime.scheduler import SchedulerPolicy, effective_priority
from aether.storage.database import Database
from aether.storage.models import EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories import (
    ObjectiveRepository,
    TaskDependencyRepository,
    TaskRepository,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    db = schema_database(tmp_path / "work_queue.db")
    yield db
    await db.close()


@pytest.fixture
def sessions(database: Database) -> async_sessionmaker[AsyncSession]:
    return database.sessions


@pytest.fixture
def service(sessions: async_sessionmaker[AsyncSession]) -> WorkQueueService:
    return WorkQueueService(sessions, clock=lambda: NOW)


@pytest_asyncio.fixture
async def objective_id(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    return await ObjectiveRepository(sessions).create(title="Work queue objective")


async def make_task(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: UUID,
    agent: AgentId | None,
    *,
    title: str = "task",
    priority: int = 50,
    state: TaskStatus = TaskStatus.READY,
    waiting_hours: float = 0.0,
    deadline: datetime | None = None,
    key: str | None = None,
) -> UUID:
    tasks = TaskRepository(sessions)
    task_id = await tasks.create(
        objective_id=objective_id,
        title=title,
        assigned_agent=agent.value if agent else None,
        priority=priority,
        deadline=deadline,
        idempotency_key=key,
    )
    path = {
        TaskStatus.PENDING: [],
        TaskStatus.READY: [TaskStatus.READY],
        TaskStatus.BLOCKED: [TaskStatus.BLOCKED],
        TaskStatus.WAITING_FOR_APPROVAL: [
            TaskStatus.READY,
            TaskStatus.SCHEDULED,
            TaskStatus.RUNNING,
            TaskStatus.WAITING_FOR_APPROVAL,
        ],
        TaskStatus.COMPLETED: [
            TaskStatus.READY,
            TaskStatus.SCHEDULED,
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
        ],
        TaskStatus.FAILED: [
            TaskStatus.READY,
            TaskStatus.SCHEDULED,
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
        ],
        TaskStatus.CANCELLED: [TaskStatus.CANCELLED],
    }[state]
    for step in path:
        await tasks.transition(task_id, step, actor="test")
    stamp = NOW - timedelta(hours=waiting_hours)
    async with sessions() as session, session.begin():
        await session.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task_id)
            .values(created_at=stamp, updated_at=stamp)
        )
    return task_id


async def denial_events(sessions: async_sessionmaker[AsyncSession]) -> list[EventRecord]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "task.access_denied")
            )
        )


async def test_open_tasks_lists_only_own_open_tasks(sessions, service, objective_id) -> None:
    mine = await make_task(sessions, objective_id, AgentId.FORGE, title="mine")
    blocked = await make_task(
        sessions, objective_id, AgentId.FORGE, title="blocked", state=TaskStatus.BLOCKED
    )
    await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.COMPLETED)
    await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.FAILED)
    await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.CANCELLED)
    await make_task(sessions, objective_id, AgentId.SCOUT, title="theirs")
    await make_task(sessions, objective_id, None, title="unassigned")

    items = await service.open_tasks(AgentId.FORGE)

    assert {item.id for item in items} == {mine, blocked}
    assert all(item.assigned_agent == "FORGE" for item in items)
    assert all(item.is_open for item in items)


async def test_open_tasks_state_filter_must_stay_open(sessions, service, objective_id) -> None:
    await make_task(sessions, objective_id, AgentId.FORGE)
    blocked = await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.BLOCKED)

    items = await service.open_tasks(AgentId.FORGE, states={TaskStatus.BLOCKED})

    assert [item.id for item in items] == [blocked]
    with pytest.raises(ValueError, match="open states"):
        await service.open_tasks(AgentId.FORGE, states={TaskStatus.COMPLETED})


async def test_finished_tasks_are_explicit_bounded_and_newest_first(
    sessions, service, objective_id
) -> None:
    await make_task(sessions, objective_id, AgentId.FORGE)
    done = await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.COMPLETED)
    failed = await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.FAILED)
    cancelled = await make_task(sessions, objective_id, AgentId.FORGE, state=TaskStatus.CANCELLED)
    await make_task(sessions, objective_id, AgentId.SCOUT, state=TaskStatus.COMPLETED)

    default = await service.finished_tasks(AgentId.FORGE)
    assert {item.id for item in default} == {done, failed}
    with_cancelled = await service.finished_tasks(AgentId.FORGE, include_cancelled=True)
    assert {item.id for item in with_cancelled} == {done, failed, cancelled}
    only_failed = await service.finished_tasks(AgentId.FORGE, states={TaskStatus.FAILED})
    assert [item.id for item in only_failed] == [failed]
    assert len(await service.finished_tasks(AgentId.FORGE, limit=1)) == 1

    for bad in (0, MAX_LIMIT + 1):
        with pytest.raises(ValueError, match="limit"):
            await service.finished_tasks(AgentId.FORGE, limit=bad)
    with pytest.raises(ValueError, match="finished states"):
        await service.finished_tasks(AgentId.FORGE, states={TaskStatus.READY})


async def test_order_is_effective_priority_then_age(sessions, service, objective_id) -> None:
    low_old = await make_task(
        sessions, objective_id, AgentId.FORGE, title="low old", priority=40, waiting_hours=20
    )
    high_new = await make_task(sessions, objective_id, AgentId.FORGE, title="high new", priority=55)
    urgent = await make_task(
        sessions,
        objective_id,
        AgentId.FORGE,
        title="urgent",
        priority=20,
        deadline=NOW - timedelta(minutes=1),
    )
    tie_old = await make_task(
        sessions, objective_id, AgentId.FORGE, title="tie old", priority=10, waiting_hours=1
    )
    tie_new = await make_task(sessions, objective_id, AgentId.FORGE, title="tie new", priority=11)

    items = await service.open_tasks(AgentId.FORGE)

    # urgent: 20 + 50 overdue = 70; low old: 40 + 20 aging = 60; high new: 55; ties: 11 each.
    assert [item.id for item in items] == [urgent, low_old, high_new, tie_old, tie_new]
    policy = SchedulerPolicy()
    assert items[0].effective_priority == effective_priority(
        20, waiting_since=NOW, now=NOW, deadline=NOW - timedelta(minutes=1), policy=policy
    )
    assert items[1].effective_priority == pytest.approx(60.0)
    assert queue_order(reversed(items)) == items
    assert [item.id for item in await service.open_tasks(AgentId.FORGE, limit=2)] == [
        urgent,
        low_old,
    ]


async def test_dependency_status_and_readiness(sessions, service, objective_id) -> None:
    upstream_done = await make_task(
        sessions, objective_id, AgentId.SCOUT, state=TaskStatus.COMPLETED
    )
    upstream_open = await make_task(sessions, objective_id, AgentId.SCOUT)
    waiting = await make_task(sessions, objective_id, AgentId.FORGE, title="waiting")
    free = await make_task(sessions, objective_id, AgentId.FORGE, title="free")
    dependencies = TaskDependencyRepository(sessions)
    await dependencies.add(waiting, upstream_done, actor="test")
    await dependencies.add(waiting, upstream_open, actor="test")
    await dependencies.add(free, upstream_done, actor="test")

    items = {item.id: item for item in await service.open_tasks(AgentId.FORGE)}

    assert items[waiting].blocked_by == (upstream_open,)
    assert not items[waiting].ready_to_run
    assert {dep.task_id for dep in items[waiting].depends_on} == {upstream_done, upstream_open}
    assert items[free].blocked_by == ()
    assert items[free].ready_to_run


async def test_incomplete_reports_missing_and_open_tasks_in_input_order(
    sessions, service, objective_id
) -> None:
    done = await make_task(sessions, objective_id, AgentId.SCOUT, state=TaskStatus.COMPLETED)
    open_task = await make_task(sessions, objective_id, AgentId.SCOUT)
    missing = uuid4()

    assert await service.incomplete([missing, done, open_task, missing]) == (missing, open_task)
    assert await service.incomplete([]) == ()


async def test_cross_agent_detail_is_denied_and_recorded(sessions, service, objective_id) -> None:
    theirs = await make_task(sessions, objective_id, AgentId.SCOUT, title="secret plan")

    with pytest.raises(TaskAccessDenied) as denied:
        await service.task_detail(AgentId.FORGE, theirs)

    assert denied.value.agent_id is AgentId.FORGE
    events = await denial_events(sessions)
    assert len(events) == 1
    event = events[0]
    assert event.actor == "FORGE"
    assert event.task_id == theirs
    assert event.payload["owner"] == "SCOUT"
    assert event.payload["operation"] == "read_task"
    assert "secret plan" not in str(event.payload)


async def test_own_and_privileged_detail_is_allowed(sessions, service, objective_id) -> None:
    task_id = await make_task(sessions, objective_id, AgentId.SCOUT, title="research")

    own = await service.task_detail(AgentId.SCOUT, task_id)
    assert own is not None and own.title == "research" and own.description == ""
    for reader in (AgentId.ORCHESTR, AgentId.LEDGER):
        detail = await service.task_detail(reader, task_id)
        assert detail is not None and detail.id == task_id
    assert await service.task_detail(AgentId.FORGE, uuid4()) is None
    assert await denial_events(sessions) == []


async def test_task_by_key_finds_own_and_denies_foreign(sessions, service, objective_id) -> None:
    mine = await make_task(sessions, objective_id, AgentId.SCOUT, key="research:SCOUT:1")
    await make_task(sessions, objective_id, AgentId.ROGUE, key="research:ROGUE:1")

    found = await service.task_by_key(AgentId.SCOUT, "research:SCOUT:1")
    assert found is not None and found.id == mine
    assert await service.task_by_key(AgentId.SCOUT, "absent") is None
    with pytest.raises(TaskAccessDenied):
        await service.task_by_key(AgentId.SCOUT, "research:ROGUE:1")
    assert len(await denial_events(sessions)) == 1


async def test_waiting_for_approval_is_limited_to_approval_readers(
    sessions, service, objective_id
) -> None:
    waiting = await make_task(
        sessions, objective_id, AgentId.FORGE, state=TaskStatus.WAITING_FOR_APPROVAL
    )
    await make_task(sessions, objective_id, AgentId.FORGE)

    for reader in (AgentId.WARDEN, AgentId.ORCHESTR, AgentId.LEDGER):
        assert [item.id for item in await service.waiting_for_approval(reader)] == [waiting]
    with pytest.raises(TaskAccessDenied):
        await service.waiting_for_approval(AgentId.MUSE)
    events = await denial_events(sessions)
    assert [event.payload["operation"] for event in events] == ["list_waiting_for_approval"]


async def test_facade_is_read_only_and_bound(sessions, service, objective_id) -> None:
    task_id = await make_task(sessions, objective_id, AgentId.FORGE)
    queue = AgentWorkQueue(AgentId.FORGE, service)

    assert [item.id for item in await queue.open_tasks()] == [task_id]
    with pytest.raises(AttributeError):
        queue.agent_id = AgentId.ORCHESTR  # type: ignore[misc]
    assert queue.rebind(sessions) is queue
    assert "FORGE" in repr(queue)


async def test_work_queue_helper_binds_identity_and_sessions(sessions, service) -> None:
    ctx = AgentContext(sessions=sessions, bus=MessageBus(sessions), clock=lambda: NOW)

    fallback = work_queue(ctx, AgentId.SCOUT)
    assert fallback.agent_id is AgentId.SCOUT

    bound = AgentContext(
        sessions=sessions,
        bus=MessageBus(sessions),
        work=AgentWorkQueue(AgentId.FORGE, service),
    )
    assert work_queue(bound, AgentId.FORGE) is bound.work
    with pytest.raises(TaskAccessDenied):
        work_queue(bound, AgentId.ORCHESTR)
