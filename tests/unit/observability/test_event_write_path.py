"""Every audit write goes through build_event + EventRecorder.add in the caller's transaction."""

import re
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.observability.events import EVENT_CATEGORIES, EVENT_TYPES
from aether.observability.recorder import EventRecorder
from aether.runtime.leases import LeaseManager
from aether.storage.database import Database
from aether.storage.models import EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories import ObjectiveRepository, TaskRepository

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "aether"
ALLOWED_ROOT = SOURCE_ROOT / "observability"


def test_event_record_is_only_constructed_inside_observability() -> None:
    offenders = [
        path.relative_to(SOURCE_ROOT).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if ALLOWED_ROOT not in path.parents
        and re.search(r"(?<!class )\bEventRecord\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


async def _all_events(database: Database) -> list[EventRecord]:
    async with database.sessions() as session:
        return list(await session.scalars(select(EventRecord).order_by(EventRecord.created_at)))


async def _task_created(database: Database, objective_id: UUID):
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id, title="Write path"
    )
    return "task.created", {"task_id": task_id, "objective_id": objective_id}


async def _task_transitioned(database: Database, objective_id: UUID):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(objective_id=objective_id, title="Write path")
    await repository.transition(task_id, TaskStatus.READY, actor="ORCHESTR")
    return "task.transitioned", {"task_id": task_id, "objective_id": objective_id}


async def _lease_acquired(database: Database, objective_id: UUID):
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(
        objective_id=objective_id, title="Write path", assigned_agent="FORGE"
    )
    await repository.transition(task_id, TaskStatus.READY, actor="ORCHESTR")
    assert await LeaseManager(database.sessions).acquire(task_id, "worker-1", timedelta(60))
    return "task.lease_acquired", {"task_id": task_id, "objective_id": objective_id}


async def _message_published(database: Database, objective_id: UUID):
    message = AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.TASK_ASSIGN,
        objective_id=objective_id,
        payload={"note": "hello"},
    )
    assert await MessageBus(database.sessions).publish(message)
    return "message.published", {
        "objective_id": objective_id,
        "correlation_id": message.correlation_id,
        "aggregate_id": message.message_id,
    }


async def _objective_priority(database: Database, objective_id: UUID):
    await ObjectiveRepository(database.sessions).update_priority(
        objective_id, 90, actor="ORCHESTR", reason="urgent"
    )
    return "objective.priority_changed", {
        "objective_id": objective_id,
        "aggregate_id": objective_id,
    }


@pytest.mark.parametrize(
    "scenario",
    [_task_created, _task_transitioned, _lease_acquired, _message_published, _objective_priority],
)
async def test_operation_writes_one_typed_event_with_dedicated_columns(
    database: Database, scenario
) -> None:
    objective_id = await ObjectiveRepository(database.sessions).create(title="Write path")
    baseline = {event.id for event in await _all_events(database)}

    event_type, expected = await scenario(database, objective_id)

    fresh = [event for event in await _all_events(database) if event.id not in baseline]
    matching = [event for event in fresh if event.event_type == event_type]
    assert len(matching) == 1
    event = matching[0]
    assert event.category == EVENT_CATEGORIES[event_type].value
    assert event.source
    assert event.correlation_id is not None
    for column, value in expected.items():
        assert getattr(event, column) == value
    # Metadata lives in columns; the payload is exactly the typed model.
    assert "meta" not in event.payload
    EVENT_TYPES[event_type].model_validate(event.payload)


async def test_all_events_from_a_scenario_validate_against_the_catalogue(
    database: Database,
) -> None:
    objective_id = await ObjectiveRepository(database.sessions).create(title="Catalogue")
    await _lease_acquired(database, objective_id)
    await _message_published(database, objective_id)
    events = await _all_events(database)
    assert len(events) >= 4
    for event in events:
        assert event.event_type in EVENT_TYPES
        assert event.category == EVENT_CATEGORIES[event.event_type].value
        assert event.source and event.correlation_id is not None
        EVENT_TYPES[event.event_type].model_validate(event.payload)


async def test_event_failure_rolls_back_the_state_change(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    objective_id = await ObjectiveRepository(database.sessions).create(title="Atomic")

    def explode(self, session, event):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(EventRecorder, "add", explode)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        await TaskRepository(database.sessions).create(objective_id=objective_id, title="Lost")

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRecord)) == 0


async def test_lease_acquire_rolls_back_when_its_event_cannot_be_staged(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    objective_id = await ObjectiveRepository(database.sessions).create(title="Lease")
    repository = TaskRepository(database.sessions)
    task_id = await repository.create(
        objective_id=objective_id, title="Leased", assigned_agent="FORGE"
    )
    await repository.transition(task_id, TaskStatus.READY, actor="ORCHESTR")

    def explode(self, session, event):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(EventRecorder, "add", explode)
    with pytest.raises(RuntimeError):
        await LeaseManager(database.sessions).acquire(task_id, "worker-1", timedelta(60))
    monkeypatch.undo()

    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id, populate_existing=True)
    assert task is not None and task.state == TaskStatus.READY.value
