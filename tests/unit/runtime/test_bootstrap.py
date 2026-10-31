"""Startup wiring: spool replay, logging, retention, and the runtime.started event."""

import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlite_schema import copy_migrated, copy_schema_to

from aether import main as cli
from aether.config import AetherSettings
from aether.observability.events import build_event
from aether.observability.spool import EventSpool
from aether.runtime.bootstrap import build_runtime, spool_path
from aether.storage.models import EventRecord

SECRET = "pw-should-never-appear"


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


def _spooled_event():
    return build_event(
        "error.raised",
        source="unit-test",
        actor="system",
        aggregate_type="runtime",
        aggregate_id=uuid4(),
        payload={
            "error_type": "Offline",
            "message": "recorded while the database was down",
            "details": {"password": SECRET},
        },
    )


async def test_start_replays_spool_before_runtime_started_and_is_idempotent(
    tmp_path: Path, isolated_logging: None
) -> None:
    settings = _settings(tmp_path)
    runtime = build_runtime(settings)
    copy_schema_to(settings.database_url.get_secret_value())
    try:
        assert runtime.recorder.spool is not None
        assert runtime.recorder.spool.path == spool_path(settings).resolve()
        spooled = _spooled_event()
        runtime.recorder.spool.append(spooled)
        assert SECRET not in runtime.recorder.spool.path.read_text(encoding="utf-8")

        await runtime.run(once=True)

        assert runtime.spool_replay is not None
        assert (runtime.spool_replay.replayed, runtime.spool_replay.failed) == (1, 0)
        async with runtime.database.sessions() as session:
            events = list(
                await session.scalars(select(EventRecord).order_by(EventRecord.created_at))
            )
        by_type = {event.event_type: event for event in events}
        assert by_type["error.raised"].id == spooled.event_id
        assert by_type["error.raised"].category == "error"
        assert by_type["error.raised"].source == "unit-test"
        assert by_type["runtime.started"].payload["spool_replayed"] == 1
        assert by_type["runtime.started"].payload["spool_failed"] == 0
        assert "runtime.stopped" in by_type
        # Replayed events precede runtime.started: work is only accepted after replay.
        assert events.index(by_type["error.raised"]) < events.index(by_type["runtime.started"])

        again = await EventSpool(spool_path(settings)).replay(runtime.database.sessions)
        assert (again.replayed, again.failed) == (0, 0)
        async with runtime.database.sessions() as session:
            count = len(list(await session.scalars(select(EventRecord.id))))
        assert count == len(events)
    finally:
        await runtime.close()


async def test_start_configures_logging_and_applies_retention(
    tmp_path: Path, isolated_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aether.runtime import bootstrap

    calls: list[tuple[Path, int]] = []

    def fake_retention(log_dir, *, archive_after_days, delete_archives_after_days, now=None):
        calls.append((Path(log_dir), archive_after_days))
        return bootstrap.RetentionResult(archived=0, deleted=0)

    monkeypatch.setattr(bootstrap, "apply_retention", fake_retention)
    settings = _settings(tmp_path)
    runtime = build_runtime(settings)
    copy_schema_to(settings.database_url.get_secret_value())
    try:
        await runtime.run(once=True)
    finally:
        await runtime.close()

    assert calls == [(settings.log_dir, settings.log_retention_days)]
    log_text = (settings.log_dir / "runtime.log").read_text(encoding="utf-8")
    assert "runtime.started" in log_text and "runtime.stopped" in log_text


def _migrate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    database_path = tmp_path / "cli.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_LOG_DIR", str(tmp_path / "logs"))
    return copy_migrated(database_path)


def test_cli_run_once_starts_records_events_and_exits_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_logging: None,
) -> None:
    database_path = _migrate(monkeypatch, tmp_path)

    cli.main(["run", "--once"])

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT event_type, category, source, correlation_id FROM events ORDER BY created_at"
        ).fetchall()
    types = [row[0] for row in rows]
    assert "runtime.started" in types and "runtime.stopped" in types
    assert types.index("runtime.started") < types.index("runtime.stopped")
    runtime_rows = [row for row in rows if row[0].startswith("runtime.")]
    assert all(
        row[1] == "runtime" and row[2] == "runtime.engine" and row[3] for row in runtime_rows
    )
    resource_rows = [row for row in rows if row[0] == "resource.measured"]
    assert len(resource_rows) == 1
    assert resource_rows[0][1:3] == ("resource", "storage.resources")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM resource_measurements").fetchone()[0] == 1
    assert (tmp_path / "logs" / "runtime.log").exists()
    assert capsys.readouterr().err == ""


def test_cli_run_duration_stops_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_logging: None
) -> None:
    database_path = _migrate(monkeypatch, tmp_path)

    cli.main(["run", "--duration", "0.3"])

    with sqlite3.connect(database_path) as connection:
        types = {row[0] for row in connection.execute("SELECT event_type FROM events")}
    assert {"runtime.started", "runtime.stopping", "runtime.stopped"} <= types


def test_cli_events_tail_prints_recent_events_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    isolated_logging: None,
) -> None:
    _migrate(monkeypatch, tmp_path)
    cli.main(["run", "--once"])
    capsys.readouterr()

    cli.main(["events", "tail", "--limit", "1"])
    last = capsys.readouterr().out.strip().splitlines()
    assert len(last) == 1 and "runtime.stopped" in last[0]

    cli.main(["events", "tail"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert "resource.measured" in lines[0]
    assert "runtime.security_initialized" in lines[1]
    assert "runtime.models_initialized" in lines[2]
    assert "runtime.started" in lines[3]
    assert "runtime.stopped" in lines[-1]


def test_cli_run_never_prints_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", f"postgresql://aether:{SECRET}@127.0.0.1:1/aether")
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_LOG_DIR", str(tmp_path / "logs"))

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--once"])

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    for path in (tmp_path / "logs").rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.parametrize("argument", ["0", "-1", "abc", "inf", "nan"])
def test_cli_run_rejects_invalid_duration(argument: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--duration", argument])
    assert exit_info.value.code == 2


async def test_build_runtime_accepts_explicit_engine_limits(tmp_path: Path) -> None:
    from aether.runtime.engine import EngineLimits

    limits = EngineLimits(message_poll_seconds=0.05, scheduler_interval_seconds=0.1)
    runtime = build_runtime(_settings(tmp_path), engine_limits=limits)
    try:
        assert runtime.engine._limits is limits
    finally:
        await runtime.close()

    default = build_runtime(_settings(tmp_path))
    try:
        assert default.engine._limits.message_poll_seconds != 0.05
    finally:
        await default.close()
