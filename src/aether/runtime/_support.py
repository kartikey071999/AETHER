"""Small helpers shared by runtime and communication internals."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.storage.models import EventRecord

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalise to aware UTC; SQLite returns naive values that were stored as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def add_event(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    actor: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> None:
    """Stage an audit event in the caller's transaction (swappable for the shared recorder)."""
    event = EventRecord(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        actor=actor,
        payload=payload or {},
        idempotency_key=idempotency_key,
    )
    if created_at is not None:
        event.created_at = created_at
    session.add(event)
