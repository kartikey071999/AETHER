"""Fsynced JSONL fallback with idempotent, recoverable replay."""

import logging
import os
import tempfile
import threading
from _thread import LockType
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import AetherEvent
from aether.observability.recorder import split_legacy_payload
from aether.observability.redaction import redact, redact_string
from aether.storage.models import EventRecord

_LOCKS: dict[Path, LockType] = {}
_LOCKS_GUARD = threading.Lock()
_LOGGER = logging.getLogger("aether.error")


@dataclass(frozen=True)
class ReplayResult:
    replayed: int
    failed: int


class SpoolReplayError(RuntimeError):
    pass


class _SpoolRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    event_type: str = Field(min_length=1, max_length=64)
    category: str = Field(default="general", min_length=1, max_length=32)
    aggregate_type: str = Field(min_length=1, max_length=32)
    aggregate_id: UUID
    actor: str = Field(min_length=1, max_length=24)
    source: str | None = Field(default=None, max_length=64)
    correlation_id: UUID | None = None
    objective_id: UUID | None = None
    task_id: UUID | None = None
    payload: dict[str, Any]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def lift_legacy_metadata(cls, value: Any) -> Any:
        """Entries spooled before the dedicated event columns wrapped metadata in the payload."""
        if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
            return value
        meta, data = split_legacy_payload(value["payload"])
        if not meta:
            return value
        lifted = {
            name: meta[name]
            for name in ("category", "source", "correlation_id", "objective_id", "task_id")
            if meta.get(name) is not None and name not in value
        }
        return {**value, **lifted, "payload": data}

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("spool timestamp must include a timezone")
        return value.astimezone(UTC)


class EventSpool:
    """One local append-only spool; serialize replay across processes externally.

    Appends use a single OS write under a shared in-process path lock. Replay
    preserves concurrent appends, and detects concurrent rewrites rather than
    discarding unseen data. Corrupt entries stay on disk and are reported.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        lock_path = str(self.path)
        # Windows resolve() can return either spelling as a missing file appears.
        if os.name == "nt":
            if lock_path.upper().startswith("\\\\?\\UNC\\"):
                lock_path = "\\\\" + lock_path[8:]
            elif lock_path.startswith("\\\\?\\"):
                lock_path = lock_path[4:]
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(Path(lock_path), threading.Lock())

    def append(self, event: AetherEvent) -> None:
        from aether.observability.recorder import _to_record

        record = _to_record(event)
        document = _SpoolRecord(
            id=record.id,
            event_type=record.event_type,
            category=record.category,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            actor=record.actor,
            source=record.source,
            correlation_id=record.correlation_id,
            objective_id=record.objective_id,
            task_id=record.task_id,
            payload=record.payload,
            idempotency_key=record.idempotency_key,
            created_at=record.created_at,
        )
        line = (document.model_dump_json() + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self.path.exists() and self.path.stat().st_size:
                with self.path.open("rb") as stream:
                    stream.seek(-1, os.SEEK_END)
                    if stream.read(1) != b"\n":
                        line = b"\n" + line
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
            descriptor = os.open(self.path, flags, 0o600)
            try:
                if os.write(descriptor, line) != len(line):
                    raise OSError("Incomplete event spool write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    async def replay(self, sessions: async_sessionmaker[AsyncSession]) -> ReplayResult:
        from aether.observability.recorder import _persist_record

        with self._lock:
            if not self.path.exists():
                return ReplayResult(replayed=0, failed=0)
            original = self.path.read_bytes()
        retained: list[bytes] = []
        replayed = 0
        for number, line in enumerate(original.splitlines(keepends=True), start=1):
            try:
                document = _SpoolRecord.model_validate_json(line)
                record = EventRecord(**document.model_dump())
                record.payload = redact(record.payload)
                record.actor = redact_string(record.actor)
                await _persist_record(sessions, record)
            except (ValidationError, SQLAlchemyError):
                retained.append(line)
                _LOGGER.error("Event spool replay failed", extra={"spool_line": number})
            else:
                replayed += 1
        if not replayed:
            return ReplayResult(replayed=0, failed=len(retained))
        with self._lock:
            current = self.path.read_bytes()
            if not current.startswith(original):
                raise SpoolReplayError("Spool changed during replay; no entries removed")
            remaining = b"".join(retained) + current[len(original) :]
            self._replace(remaining)
        return ReplayResult(replayed=replayed, failed=len(retained))

    def _replace(self, contents: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
