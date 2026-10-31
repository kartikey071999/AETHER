"""Event persistence without changing the caller's transaction boundary."""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import AetherEvent, EventCategory, build_event
from aether.observability.redaction import redact, redact_string
from aether.storage.models import EventRecord

if TYPE_CHECKING:
    from aether.observability.spool import EventSpool


class EventWriteError(RuntimeError):
    """The database write failed; the event was spooled unless stated otherwise."""


def _to_record(event: AetherEvent) -> EventRecord:
    """The only mapping from typed envelopes to the current storage schema."""
    document = event.model_dump(mode="json")
    return EventRecord(
        id=event.event_id,
        event_type=event.event_type,
        category=event.category.value,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        actor=redact_string(str(event.actor)),
        source=redact_string(event.source),
        correlation_id=event.correlation_id,
        objective_id=event.objective_id,
        task_id=event.task_id,
        session_id=str(event.session_id) if event.session_id is not None else None,
        branch=redact_string(event.branch) if event.branch is not None else None,
        payload=redact(document["payload"]),
        idempotency_key=event.idempotency_key,
        created_at=event.timestamp,
    )


def split_legacy_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(metadata, data)``; older rows wrapped both in ``{"meta": ..., "data": ...}``."""
    meta, data = payload.get("meta"), payload.get("data")
    if set(payload) == {"meta", "data"} and isinstance(meta, dict) and isinstance(data, dict):
        return meta, data
    return {}, dict(payload)


async def _find_existing(session: AsyncSession, record: EventRecord) -> EventRecord | None:
    conditions = [EventRecord.id == record.id]
    if record.idempotency_key is not None:
        conditions.append(EventRecord.idempotency_key == record.idempotency_key)
    return await session.scalar(select(EventRecord).where(or_(*conditions)))


async def _persist_record(
    sessions: async_sessionmaker[AsyncSession], record: EventRecord
) -> EventRecord:
    try:
        async with sessions() as session, session.begin():
            existing = await _find_existing(session, record)
            if existing is not None:
                session.expunge(existing)
                return existing
            session.add(record)
            await session.flush()
            # Detach before commit so expire_on_commit=True factories work too.
            session.expunge(record)
        return record
    except IntegrityError:
        # A concurrent writer may win after the initial lookup.
        async with sessions() as session:
            existing = await _find_existing(session, record)
            if existing is not None:
                session.expunge(existing)
                return existing
        raise


def stage_event(
    session: AsyncSession,
    event_type: str,
    *,
    source: str,
    actor: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: Mapping[str, Any] | BaseModel | None = None,
    category: str | None = None,
    correlation_id: UUID | None = None,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> EventRecord:
    """Validate a catalogued event and stage it in the caller's transaction.

    The event commits or rolls back with the surrounding state change; nothing is flushed here.
    """
    optional = {
        "category": EventCategory(str(category)) if category is not None else None,
        "correlation_id": correlation_id,
        "objective_id": objective_id,
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "timestamp": created_at,
    }
    event = build_event(
        event_type,
        source=source,
        actor=str(actor),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload if payload is not None else {},
        **{name: value for name, value in optional.items() if value is not None},
    )
    return EventRecorder().add(session, event)


class EventRecorder:
    def __init__(self, spool: "EventSpool | None" = None) -> None:
        self.spool = spool

    def add(self, session: AsyncSession, event: AetherEvent) -> EventRecord:
        """Add without flushing/committing; caller owns rollback and write failures.

        Duplicate keys in this API are enforced by the database at caller flush.
        Use ``record`` for automatic duplicate lookup and failure spooling.
        """
        record = _to_record(event)
        session.add(record)
        return record

    async def record(
        self, sessions: async_sessionmaker[AsyncSession], event: AetherEvent
    ) -> EventRecord:
        """Commit independently; duplicate key/event ID returns the existing record.

        A duplicate key is first-write-wins, even if its new payload differs.
        Database failures always raise, including when the fallback spool fails.
        """
        record = _to_record(event)
        try:
            return await _persist_record(sessions, record)
        except SQLAlchemyError as database_error:
            if self.spool is None:
                raise EventWriteError(
                    "Event database write failed and no fallback spool is configured"
                ) from database_error
            try:
                self.spool.append(event)
            except OSError as spool_error:
                raise EventWriteError(
                    "Event database write failed and fallback spool write also failed"
                ) from spool_error
            raise EventWriteError(
                "Event database write failed; event saved to spool"
            ) from database_error
