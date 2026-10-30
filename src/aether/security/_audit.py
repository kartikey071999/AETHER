"""Typed security audit events plus matching security log lines."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.observability.events import AetherEvent, EventCategory, build_event
from aether.observability.logging import get_logger
from aether.observability.recorder import EventRecorder

SOURCE = "aether.security"


def stage_event(
    session: AsyncSession,
    recorder: EventRecorder,
    event_type: str,
    *,
    actor: str,
    aggregate_type: str,
    aggregate_id: UUID,
    payload: dict[str, Any],
    source: str = SOURCE,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    correlation_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> AetherEvent:
    """Validate and stage an event inside the caller's transaction."""
    fields: dict[str, Any] = {
        "source": source,
        "actor": str(actor),
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "objective_id": objective_id,
        "task_id": task_id,
        "idempotency_key": idempotency_key,
        "payload": payload,
    }
    if correlation_id is not None:
        fields["correlation_id"] = correlation_id
    event = build_event(event_type, **fields)
    recorder.add(session, event)
    return event


def log_security(event: AetherEvent, level: int = logging.INFO, **extra: Any) -> None:
    """Security log line for an event that has already been committed."""
    get_logger(EventCategory.SECURITY).log(
        level, event.event_type, extra={"event": event.model_dump(mode="json"), **extra}
    )
