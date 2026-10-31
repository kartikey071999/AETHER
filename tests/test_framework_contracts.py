from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlite_schema import copy_schema_to

from aether.agents import AGENT_DEFINITIONS, get_agent
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.config import AetherSettings
from aether.runtime.resource_monitor import HostResources
from aether.runtime.scheduler import (
    Scheduler,
    SchedulerPolicy,
    SchedulerResources,
    SchedulingCandidate,
)
from aether.storage.database import Database
from aether.storage.models import EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories import (
    InvalidTaskTransition,
    ObjectiveRepository,
    TaskRepository,
)


def test_registry_contains_seven_distinct_roles() -> None:
    assert {definition.agent_id for definition in AGENT_DEFINITIONS} == set(AgentId)
    assert len({definition.responsibility for definition in AGENT_DEFINITIONS}) == 7
    assert get_agent(AgentId.ROGUE).agent_id is AgentId.ROGUE


def test_message_envelope_generates_ids_and_rejects_naive_timestamps() -> None:
    message = AgentMessage(
        source_agent=AgentId.ROGUE,
        target_agent=AgentId.SCOUT,
        message_type=MessageType.RESEARCH_FINDING,
        payload={"finding": "candidate approach"},
    )

    assert message.message_id
    assert message.correlation_id
    assert message.timestamp.tzinfo == UTC

    with pytest.raises(ValidationError):
        AgentMessage(
            source_agent=AgentId.ROGUE,
            target_agent=AgentId.SCOUT,
            message_type=MessageType.RESEARCH_FINDING,
            timestamp=datetime(2026, 1, 1),
            payload={},
        )


@pytest.mark.parametrize(
    "deadline",
    [
        None,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30))),
    ],
)
def test_message_accepts_missing_or_timezone_aware_deadline(deadline: datetime | None) -> None:
    message = AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.TASK_ASSIGN,
        payload={},
        deadline=deadline,
    )

    assert message.deadline == deadline


def test_message_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentMessage(
            source_agent=AgentId.ORCHESTR,
            target_agent=AgentId.FORGE,
            message_type=MessageType.TASK_ASSIGN,
            payload={},
            deadline=datetime(2026, 1, 1),
        )

    assert [error["loc"] for error in exc_info.value.errors()] == [("deadline",)]


def test_scheduler_limits_are_validated() -> None:
    assert AetherSettings(max_concurrent_tasks=4).max_concurrent_tasks == 4
    with pytest.raises(ValidationError):
        AetherSettings(max_concurrent_tasks=0)


@pytest.mark.asyncio
async def test_task_state_and_audit_event_survive_database_reopen(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'aether.db').as_posix()}"
    copy_schema_to(database_url)
    database = Database(database_url)

    objective_id = await ObjectiveRepository(database.sessions).create(title="Test objective")
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id,
        title="Test task",
    )
    await TaskRepository(database.sessions).transition(
        task_id,
        TaskStatus.READY,
        actor="ORCHESTR",
    )
    await database.close()

    reopened = Database(database_url)
    async with reopened.sessions() as session:
        task = await session.get(TaskRecord, task_id)
        events = await session.scalars(
            select(EventRecord).where(EventRecord.aggregate_id == task_id)
        )

    assert task is not None
    assert task.state == TaskStatus.READY
    assert [event.event_type for event in events] == ["task.created", "task.transitioned"]
    await reopened.close()


@pytest.mark.asyncio
async def test_invalid_task_transition_does_not_change_persisted_state(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'aether-invalid.db').as_posix()}"
    copy_schema_to(database_url)
    database = Database(database_url)
    objective_id = await ObjectiveRepository(database.sessions).create(title="Test objective")
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id,
        title="Test task",
    )
    task_repository = TaskRepository(database.sessions)

    with pytest.raises(InvalidTaskTransition):
        await task_repository.transition(
            task_id,
            TaskStatus.COMPLETED,
            actor="ORCHESTR",
        )

    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id)
        events = await session.scalars(
            select(EventRecord).where(EventRecord.aggregate_id == task_id)
        )
    assert task is not None
    assert task.state == TaskStatus.PENDING
    assert [event.event_type for event in events] == ["task.created"]
    await database.close()


def test_scheduler_defers_work_that_would_breach_disk_reserve() -> None:
    now = datetime.now(UTC)
    too_large = SchedulingCandidate(
        task_id=uuid4(),
        priority=95,
        waiting_since=now,
        estimated_disk_bytes=6 * 1024**3,
    )
    fits = SchedulingCandidate(
        task_id=uuid4(),
        priority=40,
        waiting_since=now,
        estimated_disk_bytes=1024**3,
    )
    scheduler = Scheduler(SchedulerPolicy(min_free_disk_bytes=5 * 1024**3))
    resources = SchedulerResources(
        host=HostResources(
            cpu_percent=10,
            available_ram_bytes=8 * 1024**3,
            free_disk_bytes=10 * 1024**3,
        )
    )

    decision = scheduler.choose_next([too_large, fits], resources, now=now)

    assert decision.selected_task_id == fits.task_id
    assert decision.deferred[0].task_id == too_large.task_id
    assert "free-disk" in decision.deferred[0].reason


def test_scheduler_cpu_estimate_is_a_share_of_one_core() -> None:
    now = datetime.now(UTC)
    build = SchedulingCandidate(
        task_id=uuid4(), priority=50, waiting_since=now, estimated_cpu_percent=60.0
    )
    scheduler = Scheduler(SchedulerPolicy())

    def host(cpu_percent: float, cpu_count: int) -> SchedulerResources:
        return SchedulerResources(
            host=HostResources(
                cpu_percent=cpu_percent,
                available_ram_bytes=8 * 1024**3,
                free_disk_bytes=20 * 1024**3,
                cpu_count=cpu_count,
            )
        )

    # 8 cores at 30% leave 560 core-percent: a 60% single-core estimate fits.
    assert scheduler.choose_next([build], host(30, 8), now=now).selected_task_id == build.task_id
    # 1 core at 70% leaves 30 core-percent: it does not.
    busy = scheduler.choose_next([build], host(70, 1), now=now)
    assert busy.selected_task_id is None
    assert busy.deferred[0].reason.startswith("deferred: host CPU")
    # The host ceiling is a separate safety limit, whatever the estimate.
    tiny = SchedulingCandidate(
        task_id=uuid4(), priority=50, waiting_since=now, estimated_cpu_percent=1.0
    )
    hot = scheduler.choose_next([tiny], host(90, 64), now=now)
    assert hot.selected_task_id is None
    assert "ceiling" in hot.deferred[0].reason


def test_scheduler_ages_waiting_work_and_prioritizes_deadlines() -> None:
    now = datetime.now(UTC)
    older = SchedulingCandidate(
        task_id=uuid4(),
        priority=50,
        waiting_since=now - timedelta(hours=20),
    )
    newer = SchedulingCandidate(
        task_id=uuid4(),
        priority=65,
        waiting_since=now,
    )
    due_soon = SchedulingCandidate(
        task_id=uuid4(),
        priority=40,
        waiting_since=now,
        deadline=now + timedelta(minutes=30),
    )
    scheduler = Scheduler(SchedulerPolicy())
    resources = SchedulerResources(
        host=HostResources(
            cpu_percent=0,
            available_ram_bytes=8 * 1024**3,
            free_disk_bytes=20 * 1024**3,
        )
    )

    aged_decision = scheduler.choose_next([older, newer], resources, now=now)
    deadline_decision = scheduler.choose_next([newer, due_soon], resources, now=now)

    assert aged_decision.selected_task_id == older.task_id
    assert deadline_decision.selected_task_id == due_soon.task_id


@pytest.mark.asyncio
async def test_scheduler_records_decision_as_durable_event(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'aether-scheduler.db').as_posix()}"
    copy_schema_to(database_url)
    database = Database(database_url)
    scheduler = Scheduler(SchedulerPolicy(), database.sessions)
    decision = scheduler.choose_next(
        [],
        SchedulerResources(
            host=HostResources(
                cpu_percent=0,
                available_ram_bytes=8 * 1024**3,
                free_disk_bytes=20 * 1024**3,
            )
        ),
    )

    await scheduler.record_decision(decision)

    async with database.sessions() as session:
        event = await session.scalar(
            select(EventRecord).where(EventRecord.event_type == "scheduler.decision")
        )
    assert event is not None
    assert event.payload["selected_task_id"] is None
    await database.close()
