import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability import recorder as recorder_module
from aether.observability.events import AetherEvent
from aether.observability.recorder import EventRecorder, EventWriteError
from aether.observability.spool import EventSpool, ReplayResult, SpoolReplayError
from aether.storage.database import Database
from aether.storage.models import EventRecord, ObjectiveRecord


async def event_count(database: Database) -> int:
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(EventRecord))
        assert count is not None
        return count


async def test_add_does_not_flush_commit_or_spool(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    async with database.sessions() as session:
        record = recorder.add(session, event)
        assert record in session.new
        assert record.id == event.event_id
        assert record.payload["data"]["details"]["api_key"] == "***"
        assert record.payload["meta"]["category"] == "error"
        assert record.payload["meta"]["correlation_id"] == str(event.correlation_id)
        assert await event_count(database) == 0
        await session.flush()
        await session.rollback()
    assert await event_count(database) == 0
    assert not recorder.spool.path.exists()


async def test_add_commits_and_rolls_back_with_caller_state(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    async with database.sessions() as session, session.begin():
        recorder.add(session, event)
    assert await event_count(database) == 1
    objective_id = uuid4()
    with pytest.raises(RuntimeError, match="abort"):
        async with database.sessions() as session, session.begin():
            session.add(ObjectiveRecord(id=objective_id, title="Atomic objective"))
            recorder.add(session, event.model_copy(update={"event_id": uuid4()}))
            await session.flush()
            raise RuntimeError("abort")
    assert await event_count(database) == 1
    async with database.sessions() as session:
        assert await session.get(ObjectiveRecord, objective_id) is None


async def test_record_is_durable_and_returns_accessible_detached_record(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    sessions = async_sessionmaker(database.engine, expire_on_commit=True)
    record = await recorder.record(sessions, event)
    assert record.id == event.event_id
    assert record.created_at == event.timestamp
    assert record.payload["data"]["details"]["nested"]["password"] == "***"
    async with database.sessions() as session:
        stored = await session.get(EventRecord, event.event_id)
        assert stored is not None
        assert stored.payload == record.payload
    duplicate = await recorder.record(sessions, event)
    assert duplicate.id == event.event_id
    assert duplicate.payload == record.payload
    assert await event_count(database) == 1


async def test_idempotency_is_first_write_wins(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    first = event.model_copy(update={"idempotency_key": "stable-event"})
    second = event.model_copy(
        update={
            "event_id": uuid4(),
            "idempotency_key": "stable-event",
            "payload": event.payload.model_copy(update={"message": "Later duplicate"}),
        }
    )
    assert (await recorder.record(database.sessions, first)).id == first.event_id
    duplicate = await recorder.record(database.sessions, second)
    assert duplicate.id == first.event_id
    assert duplicate.payload["data"]["message"] == "safe diagnostic"
    assert await event_count(database) == 1
    assert not recorder.spool.path.exists()
    with pytest.raises(IntegrityError):
        async with database.sessions() as session, session.begin():
            recorder.add(session, second)
    assert not recorder.spool.path.exists()


async def test_concurrent_duplicate_lookup_recovers_integrity_error(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    await recorder.record(database.sessions, event)
    real_find = recorder_module._find_existing
    calls = 0

    async def race_find(session: AsyncSession, record: EventRecord) -> EventRecord | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else await real_find(session, record)

    monkeypatch.setattr(recorder_module, "_find_existing", race_find)
    assert (await recorder.record(database.sessions, event)).id == event.event_id
    assert calls == 2
    assert await event_count(database) == 1


async def test_database_failure_spools_redacted_event_and_raises(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", AsyncMock(side_effect=SQLAlchemyError("offline")))
        with pytest.raises(EventWriteError, match="saved to spool"):
            await recorder.record(database.sessions, event)
    assert await event_count(database) == 0
    text = recorder.spool.path.read_text(encoding="utf-8")
    assert "do-not-store" not in text
    assert json.loads(text)["id"] == str(event.event_id)
    assert await recorder.spool.replay(database.sessions) == ReplayResult(replayed=1, failed=0)
    assert await event_count(database) == 1
    assert recorder.spool.path.read_bytes() == b""


async def test_failed_spool_is_explicit(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = EventRecorder(EventSpool(tmp_path / "events.jsonl"))
    monkeypatch.setattr(AsyncSession, "flush", AsyncMock(side_effect=SQLAlchemyError("offline")))
    def failed_append(event: AetherEvent) -> None:
        raise OSError("Spool unavailable")

    monkeypatch.setattr(recorder.spool, "append", failed_append)
    with pytest.raises(EventWriteError, match="spool write also failed"):
        await recorder.record(database.sessions, event)


async def test_spool_replay_deduplicates_keys_and_ids(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    spool = EventSpool(tmp_path / "events.jsonl")
    keyed = event.model_copy(update={"idempotency_key": "same-key"})
    spool.append(keyed)
    spool.append(keyed)
    spool.append(keyed.model_copy(update={"event_id": uuid4()}))
    assert await spool.replay(database.sessions) == ReplayResult(replayed=3, failed=0)
    assert await spool.replay(database.sessions) == ReplayResult(replayed=0, failed=0)
    assert await event_count(database) == 1
    assert not (tmp_path / "events.jsonl").read_bytes()


async def test_replay_retains_corruption_and_database_failures(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = EventSpool(tmp_path / "events.jsonl")
    spool.path.write_bytes(b"not-json\n")
    spool.append(event)
    original = spool.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", AsyncMock(side_effect=SQLAlchemyError("offline")))
        assert await spool.replay(database.sessions) == ReplayResult(replayed=0, failed=2)
    assert spool.path.read_bytes() == original
    assert await spool.replay(database.sessions) == ReplayResult(replayed=1, failed=1)
    assert spool.path.read_bytes() == b"not-json\n"


async def test_replay_preserves_appends_during_database_write(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = EventSpool(tmp_path / "events.jsonl")
    spool.append(event)
    later = event.model_copy(update={"event_id": uuid4()})
    real_persist = recorder_module._persist_record

    async def append_during_write(sessions, record):
        EventSpool(spool.path).append(later)
        return await real_persist(sessions, record)

    with monkeypatch.context() as patch:
        patch.setattr(recorder_module, "_persist_record", append_during_write)
        assert await spool.replay(database.sessions) == ReplayResult(replayed=1, failed=0)
    assert json.loads(spool.path.read_text())["id"] == str(later.event_id)
    assert await spool.replay(database.sessions) == ReplayResult(replayed=1, failed=0)
    assert await event_count(database) == 2


async def test_replay_detects_concurrent_rewrite(
    database: Database, event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = EventSpool(tmp_path / "events.jsonl")
    spool.append(event)
    real_persist = recorder_module._persist_record

    async def rewrite_during_write(sessions, record):
        spool.path.write_bytes(b"replacement\n")
        return await real_persist(sessions, record)

    monkeypatch.setattr(recorder_module, "_persist_record", rewrite_during_write)
    with pytest.raises(SpoolReplayError, match="Spool changed"):
        await spool.replay(database.sessions)
    assert spool.path.read_bytes() == b"replacement\n"


async def test_missing_spool_replay_is_empty(database: Database, tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "missing.jsonl")
    assert await spool.replay(database.sessions) == ReplayResult(replayed=0, failed=0)
    assert not spool.path.exists()


def test_spool_concurrent_appends_are_complete_json_lines(
    event: AetherEvent, tmp_path: Path
) -> None:
    path = tmp_path / "nested" / "events.jsonl"
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: EventSpool(path).append(event), range(80)))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 80
    assert all(json.loads(line)["id"] == str(event.event_id) for line in lines)


async def test_append_after_interrupted_line_preserves_next_event(
    database: Database, event: AetherEvent, tmp_path: Path
) -> None:
    spool = EventSpool(tmp_path / "events.jsonl")
    spool.path.write_bytes(b'{"partial":')
    spool.append(event)
    assert await spool.replay(database.sessions) == ReplayResult(replayed=1, failed=1)
    assert spool.path.read_bytes() == b'{"partial":\n'


def test_spool_fsync_and_short_writes_are_reported(
    event: AetherEvent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aether.observability import spool as spool_module

    fsync = []
    real_fsync = spool_module.os.fsync

    def track_fsync(descriptor: int) -> None:
        fsync.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(spool_module.os, "fsync", track_fsync)
    spool = EventSpool(tmp_path / "events.jsonl")
    spool.append(event)
    assert len(fsync) == 1
    monkeypatch.setattr(spool_module.os, "write", lambda descriptor, data: 0)
    with pytest.raises(OSError, match="Incomplete"):
        spool.append(event)
