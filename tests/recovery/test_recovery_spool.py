"""Scenario 4: the event store is unavailable, so events fall back to the local spool.

A failed event write is never lost silently: it is appended (fsynced) to the spool and the
caller still sees an error. The next runtime start replays the spool before accepting work,
exactly once, and corrupt or torn entries stay on disk instead of being dropped.
"""

import logging
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from reliability_support import FailingCommits, create_schema
from sqlalchemy import func, select

from aether.config import AetherSettings
from aether.observability.events import AetherEvent, build_event
from aether.observability.recorder import EventRecorder, EventWriteError
from aether.observability.spool import EventSpool
from aether.runtime.bootstrap import build_runtime, spool_path
from aether.storage.database import Database
from aether.storage.models import EventRecord


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    logger = logging.getLogger("aether")
    handlers = logger.handlers[:]
    level, propagate = logger.level, logger.propagate
    logger.handlers = []
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _settings(tmp_path: Path) -> AetherSettings:
    return AetherSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'aether.db').as_posix()}",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
    )


def _error_event(message: str = "database offline") -> AetherEvent:
    return build_event(
        "error.raised",
        source="recovery-test",
        actor="system",
        aggregate_type="runtime",
        aggregate_id=uuid4(),
        payload={"error_type": "Offline", "message": message, "details": {}},
    )


async def _events(database: Database, event_type: str | None = None) -> list[EventRecord]:
    async with database.sessions() as session:
        statement = select(EventRecord)
        if event_type is not None:
            statement = statement.where(EventRecord.event_type == event_type)
        return list(await session.scalars(statement))


async def test_events_written_while_the_database_is_down_are_replayed_once_on_restart(
    tmp_path: Path, isolated_logging: None
) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.database_url.get_secret_value())
    try:
        await create_schema(database)
        flaky = FailingCommits(database.engine)
        recorder = EventRecorder(EventSpool(spool_path(settings)))
        lost, kept = _error_event("lost commit"), _error_event("written normally")

        flaky.fail_next(1)
        with pytest.raises(EventWriteError, match="saved to spool"):
            await recorder.record(flaky.sessions, lost)
        await recorder.record(flaky.sessions, kept)

        assert [event.id for event in await _events(database)] == [kept.event_id]
        spooled = spool_path(settings).read_text(encoding="utf-8").splitlines()
        assert len(spooled) == 1 and str(lost.event_id) in spooled[0]
    finally:
        await database.close()

    # Process restart: the spool is replayed before runtime.started.
    first = build_runtime(settings)
    try:
        await first.run(once=True)
        assert first.spool_replay is not None
        assert (first.spool_replay.replayed, first.spool_replay.failed) == (1, 0)
        [started] = await _events(first.database, "runtime.started")
        assert started.payload["spool_replayed"] == 1
        replayed = [e for e in await _events(first.database) if e.id == lost.event_id]
        assert len(replayed) == 1 and replayed[0].payload["message"] == "lost commit"
    finally:
        await first.close()
    assert spool_path(settings).read_bytes() == b""

    # A second restart has nothing left to replay and duplicates nothing.
    second = build_runtime(settings)
    try:
        await second.run(once=True)
        assert second.spool_replay is not None
        assert (second.spool_replay.replayed, second.spool_replay.failed) == (0, 0)
        assert len(await _events(second.database, "error.raised")) == 2
        assert len(await _events(second.database, "runtime.started")) == 2
    finally:
        await second.close()


async def test_replaying_an_event_that_also_reached_the_database_does_not_duplicate_it(
    database: Database, tmp_path: Path
) -> None:
    """The commit may succeed even though the client saw an error (and spooled the event)."""
    spool = EventSpool(tmp_path / "spool" / "events.jsonl")
    event = _error_event()
    await EventRecorder().record(database.sessions, event)
    spool.append(event)

    first = await spool.replay(database.sessions)
    second = await spool.replay(database.sessions)

    assert (first.replayed, first.failed) == (1, 0)
    assert (second.replayed, second.failed) == (0, 0)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(EventRecord)) == 1


async def test_torn_spool_entries_are_kept_while_valid_ones_replay(
    database: Database, tmp_path: Path, isolated_logging: None
) -> None:
    spool = EventSpool(tmp_path / "spool" / "events.jsonl")
    before = _error_event("before the crash")
    spool.append(before)
    with spool.path.open("ab") as stream:  # the process died in the middle of an append
        stream.write(b'{"id": "' + str(uuid4()).encode() + b'", "event_ty')
    after = _error_event("after the restart")
    spool.append(after)

    result = await spool.replay(database.sessions)

    assert (result.replayed, result.failed) == (2, 1)
    ids = {event.id for event in await _events(database)}
    assert ids == {before.event_id, after.event_id}
    retained = spool.path.read_bytes().splitlines()
    assert len(retained) == 1 and retained[0].endswith(b'"event_ty')

    again = await spool.replay(database.sessions)
    assert (again.replayed, again.failed) == (0, 1)
    assert len(await _events(database)) == 2


async def test_failure_of_both_database_and_spool_is_reported(
    database: Database, tmp_path: Path
) -> None:
    unwritable = tmp_path / "spool-is-a-directory"
    unwritable.mkdir()
    flaky = FailingCommits(database.engine)
    recorder = EventRecorder(EventSpool(unwritable))

    flaky.fail_next(1)
    with pytest.raises(EventWriteError, match="spool write also failed"):
        await recorder.record(flaky.sessions, _error_event())

    assert await _events(database) == []
