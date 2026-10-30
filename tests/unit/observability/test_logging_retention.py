import gzip
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from aether.observability.events import AetherEvent, EventCategory, build_event
from aether.observability.logging import (
    LOG_CATEGORIES,
    LoggingWriteError,
    configure_logging,
    get_logger,
    log_event,
)
from aether.observability.retention import RetentionResult, apply_retention


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.usefixtures("isolated_logging")
def test_json_logging_routes_redacts_and_is_idempotent(tmp_path: Path) -> None:
    configure_logging(tmp_path, max_bytes=100_000, backup_count=2, level="DEBUG")
    assert not list(tmp_path.iterdir())
    configure_logging(tmp_path, max_bytes=100_000, backup_count=2, level="DEBUG")
    assert len(logging.getLogger("aether").handlers) == len(LOG_CATEGORIES)
    for category in LOG_CATEGORIES:
        get_logger(category).info("password=%s", "do-not-log", extra={"api_key": "do-not-log"})
    security = read_lines(tmp_path / "security.log")
    assert len(security) == 1
    assert security[0]["message"] == "password=***"
    assert security[0]["api_key"] == "***"
    assert security[0]["level"] == "INFO"
    assert security[0]["logger"] == "aether.security"
    assert datetime.fromisoformat(security[0]["timestamp"]).utcoffset() == timedelta(0)
    get_logger("security").error("Bearer hidden")
    assert len(read_lines(tmp_path / "security.log")) == 2
    assert len(read_lines(tmp_path / "error.log")) == 2
    assert read_lines(tmp_path / "error.log")[-1]["message"] == "Bearer ***"
    get_logger("error").error("Once")
    assert len(read_lines(tmp_path / "error.log")) == 3
    assert all("do-not-log" not in path.read_text() for path in tmp_path.iterdir())


@pytest.mark.usefixtures("isolated_logging")
@pytest.mark.parametrize(
    "category,filename",
    [
        ("objective", "runtime"),
        ("task", "runtime"),
        ("message", "runtime"),
        ("capability", "agent"),
    ],
)
def test_extended_categories_use_documented_routes(
    tmp_path: Path, category: str, filename: str
) -> None:
    configure_logging(tmp_path)
    get_logger(category).info("Routed")
    assert read_lines(tmp_path / f"{filename}.log")[0]["logger"] == f"aether.{category}"


@pytest.mark.usefixtures("isolated_logging")
def test_log_event_preserves_typed_event_and_redacts(event: AetherEvent, tmp_path: Path) -> None:
    configure_logging(tmp_path)
    log_event(event)
    line = read_lines(tmp_path / "error.log")[0]
    assert line["event"]["event_id"] == str(event.event_id)
    assert line["event"]["payload"]["details"]["api_key"] == "***"
    assert "do-not-store" not in (tmp_path / "error.log").read_text()
    resource_event = build_event(
        "provider.request_completed",
        source="test",
        actor="system",
        aggregate_type="provider",
        aggregate_id=uuid4(),
        payload={"provider": "mock", "model": "mock", "tokens": 42, "cost": 0.0, "latency_ms": 10},
    )
    log_event(resource_event)
    assert read_lines(tmp_path / "resource.log")[0]["event"]["payload"]["tokens"] == 42
    assert resource_event.category is EventCategory.RESOURCE


@pytest.mark.usefixtures("isolated_logging")
def test_exception_and_console_output_are_redacted(tmp_path: Path, capsys) -> None:
    configure_logging(tmp_path, console=True)
    try:
        raise RuntimeError("Bearer exception-secret")
    except RuntimeError:
        get_logger("agent").exception(
            "ghp_message", extra={"nested": {"authorization": "exception-secret"}}
        )
    line = read_lines(tmp_path / "agent.log")[0]
    assert "exception-secret" not in json.dumps(line)
    assert "Bearer ***" in line["exception"]
    assert "exception-secret" not in capsys.readouterr().err


@pytest.mark.usefixtures("isolated_logging")
def test_rotating_logs_are_bounded_and_reconfiguration_closes_files(tmp_path: Path) -> None:
    configure_logging(tmp_path, max_bytes=400, backup_count=2)
    for number in range(20):
        get_logger("runtime").info("entry %d", number)
    assert (tmp_path / "runtime.log").exists()
    assert (tmp_path / "runtime.log.1").exists()
    assert (tmp_path / "runtime.log.2").exists()
    assert not (tmp_path / "runtime.log.3").exists()
    assert all(path.stat().st_size <= 400 for path in tmp_path.iterdir())
    old_handlers = logging.getLogger("aether").handlers[:]
    configure_logging(tmp_path / "new")
    assert all(handler.stream is None for handler in old_handlers)
    get_logger("runtime").info("new")
    assert len(read_lines(tmp_path / "new" / "runtime.log")) == 1


@pytest.mark.usefixtures("isolated_logging")
def test_logging_write_failures_raise_instead_of_disappearing(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    (tmp_path / "runtime.log").mkdir()
    with pytest.raises(LoggingWriteError, match="Failed to write"):
        get_logger("runtime").info("cannot write")


@pytest.mark.parametrize(
    "kwargs",
    [{"max_bytes": 0}, {"backup_count": 0}, {"level": "INVALID"}, {"level": -1}],
)
def test_invalid_logging_configuration_rejected(tmp_path: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        configure_logging(tmp_path, **kwargs)


def test_unknown_logger_category_rejected() -> None:
    with pytest.raises(ValueError):
        get_logger("unknown")


@pytest.mark.usefixtures("isolated_logging")
def test_errors_reach_error_log_even_at_critical_threshold(tmp_path: Path) -> None:
    configure_logging(tmp_path, level="CRITICAL")
    get_logger("security").error("Must persist")
    assert len(read_lines(tmp_path / "error.log")) == 1
    assert not (tmp_path / "security.log").exists()


@pytest.mark.usefixtures("isolated_logging")
def test_extra_fields_cannot_override_core_json_fields(tmp_path: Path) -> None:
    configure_logging(tmp_path)
    get_logger("runtime").info(
        "Actual message", extra={"timestamp": "not-a-time", "level": "fake"}
    )
    line = read_lines(tmp_path / "runtime.log")[0]
    assert line["level"] == "INFO"
    assert datetime.fromisoformat(line["timestamp"]).utcoffset() == timedelta(0)
    assert line["extra"] == {"timestamp": "not-a-time", "level": "fake"}


def age(path: Path, now: datetime, days: int) -> None:
    timestamp = (now - timedelta(days=days)).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_retention_archives_only_old_rotated_files_and_expires_archives(tmp_path: Path) -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    contents = b"old log evidence\n"
    for name in ["runtime.log", "runtime.log.1", "agent.log.2", "runtime.log.3", "events.jsonl"]:
        (tmp_path / name).write_bytes(contents)
        age(tmp_path / name, now, 10)
    age(tmp_path / "runtime.log.3", now, 1)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    expired = archive_dir / "git.log.1.gz"
    expired.write_bytes(b"old archive")
    age(expired, now, 40)
    unrelated = archive_dir / "important.gz"
    unrelated.write_bytes(b"not managed")
    age(unrelated, now, 40)
    result = apply_retention(
        tmp_path, archive_after_days=7, delete_archives_after_days=30, now=now
    )
    assert result == RetentionResult(archived=2, deleted=1)
    assert not expired.exists()
    assert unrelated.exists()
    assert (tmp_path / "runtime.log").read_bytes() == contents
    assert (tmp_path / "runtime.log.3").read_bytes() == contents
    assert (tmp_path / "events.jsonl").read_bytes() == contents
    archives = [path for path in archive_dir.glob("*.gz") if path != unrelated]
    assert len(archives) == 2
    assert all(gzip.decompress(path.read_bytes()) == contents for path in archives)
    assert all(path.stat().st_mtime == now.timestamp() for path in archives)
    assert apply_retention(
        tmp_path, archive_after_days=7, delete_archives_after_days=30, now=now
    ) == RetentionResult(archived=0, deleted=0)
    later = now + timedelta(days=31)
    assert apply_retention(
        tmp_path, archive_after_days=7, delete_archives_after_days=30, now=later
    ) == RetentionResult(archived=1, deleted=2)


def test_archive_names_do_not_overwrite_prior_rotations(tmp_path: Path) -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    for data in [b"first", b"second"]:
        path = tmp_path / "runtime.log.1"
        path.write_bytes(data)
        age(path, now, 8)
        apply_retention(tmp_path, archive_after_days=7, delete_archives_after_days=30, now=now)
    assert {gzip.decompress(path.read_bytes()) for path in (tmp_path / "archive").iterdir()} == {
        b"first",
        b"second",
    }


def test_retention_boundaries_missing_directory_and_invalid_inputs(tmp_path: Path) -> None:
    now = datetime(2026, 10, 31, tzinfo=UTC)
    kwargs = {"archive_after_days": 7, "delete_archives_after_days": 30}
    assert apply_retention(tmp_path / "absent", now=now, **kwargs) == RetentionResult(0, 0)
    path = tmp_path / "test.log.1"
    path.write_bytes(b"at threshold")
    age(path, now, 7)
    assert apply_retention(tmp_path, now=now, **kwargs).archived == 1
    with pytest.raises(ValueError, match="timezone"):
        apply_retention(tmp_path, now=datetime(2026, 1, 1), **kwargs)
    with pytest.raises(ValueError, match="negative"):
        apply_retention(tmp_path, now=now, **(kwargs | {"archive_after_days": -1}))


def test_retention_compression_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aether.observability import retention

    now = datetime(2026, 10, 31, tzinfo=UTC)
    path = tmp_path / "runtime.log.1"
    path.write_bytes(b"preserve evidence")
    age(path, now, 8)

    def failed_compression(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(retention.gzip, "GzipFile", failed_compression)
    with pytest.raises(OSError, match="disk unavailable"):
        apply_retention(tmp_path, archive_after_days=7, delete_archives_after_days=30, now=now)
    assert path.read_bytes() == b"preserve evidence"
    assert not list((tmp_path / "archive").iterdir())
