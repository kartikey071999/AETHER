"""Small helpers shared by runtime and communication internals."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.observability.recorder import stage_event

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
    source: str = "runtime",
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> None:
    """Stage a typed event in the caller's transaction via the shared recorder."""
    stage_event(
        session,
        event_type,
        source=source,
        actor=actor,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload,
        idempotency_key=idempotency_key,
        created_at=created_at,
        objective_id=objective_id,
        task_id=task_id,
        correlation_id=correlation_id,
    )
