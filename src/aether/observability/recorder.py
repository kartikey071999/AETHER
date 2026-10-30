"""Event persistence without changing the caller's transaction boundary."""

from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import AetherEvent
from aether.observability.redaction import redact, redact_string
from aether.storage.models import EventRecord

if TYPE_CHECKING:
    from aether.observability.spool import EventSpool


class EventWriteError(RuntimeError):
    """The database write failed; the event was spooled unless stated otherwise."""


def _to_record(event: AetherEvent) -> EventRecord:
    """The only mapping from typed envelopes to the current storage schema."""
    document = event.model_dump(mode="json")
    meta = {
        name: document[name]
        for name in (
            "category",
            "source",
            "objective_id",
            "task_id",
            "correlation_id",
            "session_id",
            "branch",
        )
    }
    return EventRecord(
        id=event.event_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        actor=redact_string(str(event.actor)),
        payload={"meta": redact(meta), "data": redact(document["payload"])},
        idempotency_key=event.idempotency_key,
        created_at=event.timestamp,
    )


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


class EventRecorder:
    def __init__(self, spool: "EventSpool") -> None:
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
            try:
                self.spool.append(event)
            except OSError as spool_error:
                raise EventWriteError(
                    "Event database write failed and fallback spool write also failed"
                ) from spool_error
            raise EventWriteError(
                "Event database write failed; event saved to spool"
            ) from database_error
