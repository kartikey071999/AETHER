import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.storage.models import EventRecord, ResourceMeasurementRecord, TaskRecord, TaskStatus
from aether.storage.repositories import ResourceMeasurementRepository

NOW = datetime(2030, 1, 1, tzinfo=UTC)
SAMPLE = {"cpu_percent": 12.5, "available_ram_bytes": 1234, "free_disk_bytes": 5678}


async def test_latest_range_filters_and_pagination(database, objective_id):
    repository = ResourceMeasurementRepository(database.sessions)
    assert await repository.latest() is None
    async with database.sessions() as session, session.begin():
        task = TaskRecord(objective_id=objective_id, title="linked")
        session.add(task)
        await session.flush()
        task_id = task.id
    first = await repository.record(**SAMPLE, observed_at=NOW, task_id=task_id)
    second = await repository.record(**SAMPLE, observed_at=NOW + timedelta(seconds=30))
    third = await repository.record(**SAMPLE, observed_at=NOW + timedelta(seconds=60))
    assert (await repository.latest()).id == third
    assert (await repository.latest(task_id=task_id)).id == first
    assert [row.id for row in await repository.range(limit=1, offset=1)] == [second]
    rows = await repository.range(since=NOW, until=NOW + timedelta(seconds=60))
    assert [row.id for row in rows] == [second, first]
    assert [row.id for row in await repository.range(task_id=task_id)] == [first]
    assert rows[0].cpu_percent == SAMPLE["cpu_percent"]


async def test_events_are_bounded_across_instances_and_restarts(database):
    repositories = [ResourceMeasurementRepository(database.sessions) for _ in range(3)]
    await asyncio.gather(
        *(repository.record(**SAMPLE, observed_at=NOW) for repository in repositories)
    )
    for seconds in (30, 299, 300, 301):
        await ResourceMeasurementRepository(database.sessions).record(
            **SAMPLE, observed_at=NOW + timedelta(seconds=seconds)
        )
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ResourceMeasurementRecord)))
        events = list(
            await session.scalars(
                select(EventRecord)
                .where(EventRecord.event_type == "resource.measured")
                .order_by(EventRecord.created_at)
            )
        )
    assert len(rows) == 7 and len(events) == 2
    assert [event.created_at.replace(tzinfo=UTC) for event in events] == [
        NOW,
        NOW + timedelta(seconds=300),
    ]
    assert events[0].payload == SAMPLE
    assert events[0].category == "resource"
    assert events[0].aggregate_id in {row.id for row in rows}


async def test_event_and_measurement_rollback_together(database, monkeypatch):
    def fail_event(*args, **kwargs):
        raise RuntimeError("event validation failed")

    monkeypatch.setattr("aether.storage.repositories.resources.stage_event", fail_event)
    with pytest.raises(RuntimeError, match="event validation failed"):
        await ResourceMeasurementRepository(database.sessions).record(**SAMPLE, observed_at=NOW)
    async with database.sessions() as session:
        assert list(await session.scalars(select(ResourceMeasurementRecord))) == []


@pytest.mark.parametrize("state", list(TaskStatus))
async def test_retention_preserves_every_nonterminal_task_state(database, objective_id, state):
    repository = ResourceMeasurementRepository(database.sessions)
    async with database.sessions() as session, session.begin():
        task = TaskRecord(objective_id=objective_id, title="retained", state=state.value)
        session.add(task)
        await session.flush()
        task_id = task.id
    linked = await repository.record(
        **SAMPLE, task_id=task_id, observed_at=NOW - timedelta(days=31)
    )
    await repository.record(**SAMPLE, observed_at=NOW - timedelta(days=31))
    boundary = await repository.record(**SAMPLE, observed_at=NOW - timedelta(days=30))
    recent = await repository.record(**SAMPLE, observed_at=NOW)
    terminal = state in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    assert await repository.purge(retention_days=30, now=NOW) == (2 if terminal else 1)
    remaining = {row.id for row in await repository.range()}
    assert remaining == ({boundary, recent} if terminal else {linked, boundary, recent})
    assert await repository.purge(retention_days=30, now=NOW) == 0


async def test_sample_becomes_purgeable_when_task_completes(database, objective_id):
    repository = ResourceMeasurementRepository(database.sessions)
    async with database.sessions() as session, session.begin():
        task = TaskRecord(objective_id=objective_id, title="active", state=TaskStatus.RUNNING)
        session.add(task)
        await session.flush()
        task_id = task.id
    await repository.record(**SAMPLE, task_id=task_id, observed_at=NOW - timedelta(days=60))
    assert await repository.purge(retention_days=30, now=NOW) == 0
    async with database.sessions() as session, session.begin():
        task = await session.get(TaskRecord, task_id)
        task.state = TaskStatus.COMPLETED
    assert await repository.purge(retention_days=30, now=NOW) == 1


@pytest.mark.parametrize(
    "values",
    [{"cpu_percent": -1}, {"cpu_percent": float("nan")}, {"available_ram_bytes": -1}],
)
async def test_invalid_samples_do_not_write(database, values):
    with pytest.raises(ValueError):
        await ResourceMeasurementRepository(database.sessions).record(
            **{**SAMPLE, **values}, observed_at=NOW
        )
    assert await ResourceMeasurementRepository(database.sessions).latest() is None


async def test_missing_task_does_not_write(database):
    with pytest.raises(LookupError):
        await ResourceMeasurementRepository(database.sessions).record(**SAMPLE, task_id=uuid4())
    assert await ResourceMeasurementRepository(database.sessions).latest() is None


@pytest.mark.parametrize(
    "values",
    [
        {"limit": 0},
        {"limit": 1001},
        {"offset": -1},
        {"since": NOW, "until": NOW},
        {"since": NOW.replace(tzinfo=None)},
    ],
)
async def test_invalid_range_is_explicit(database, values):
    with pytest.raises(ValueError):
        await ResourceMeasurementRepository(database.sessions).range(**values)


async def test_invalid_retention_and_naive_sample_time(database):
    repository = ResourceMeasurementRepository(database.sessions)
    with pytest.raises(ValueError):
        await repository.purge(retention_days=0)
    with pytest.raises(ValueError):
        await repository.record(**SAMPLE, observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        ResourceMeasurementRepository(database.sessions, event_interval_seconds=0)
