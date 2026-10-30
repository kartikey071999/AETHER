"""Shared helper that records audit events inside the caller's transaction."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.observability.recorder import stage_event
from aether.storage.models import EventRecord


def to_json(value: Any) -> Any:
    """Convert UUIDs, datetimes, decimals, and enums into JSON-safe primitives."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json(item) for item in value]
    return value


def as_utc(value: datetime | None) -> datetime | None:
    """Treat naive timestamps (as returned by SQLite) as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def add_event(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    actor: str,
    payload: Mapping[str, Any],
    category: str,
    correlation_id: UUID | None = None,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    idempotency_key: str | None = None,
    source: str = "storage.repositories",
) -> EventRecord:
    """Stage a typed audit event; it commits or rolls back with the surrounding transaction."""
    return stage_event(
        session,
        event_type,
        source=source,
        actor=actor,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        category=category,
        correlation_id=correlation_id,
        objective_id=objective_id,
        task_id=task_id,
        idempotency_key=idempotency_key,
    )
