"""Durable host samples with rate-limited audit events and task-aware retention."""

import math
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import ResourceMeasuredPayload
from aether.observability.recorder import stage_event
from aether.storage.models import EventRecord, ResourceMeasurementRecord, TaskRecord, TaskStatus

_TERMINAL_TASK_STATES = (
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("resource timestamps must be timezone-aware")
    return value.astimezone(UTC)


class ResourceMeasurementRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        event_interval_seconds: float = 300.0,
    ) -> None:
        if not math.isfinite(event_interval_seconds) or event_interval_seconds < 60:
            raise ValueError("resource event interval must be finite and at least 60 seconds")
        self._sessions = sessions
        self._event_interval = timedelta(seconds=event_interval_seconds)

    async def record(
        self,
        *,
        cpu_percent: float,
        available_ram_bytes: int,
        free_disk_bytes: int,
        task_id: UUID | None = None,
        observed_at: datetime | None = None,
    ) -> UUID:
        sample = ResourceMeasuredPayload(
            cpu_percent=cpu_percent,
            available_ram_bytes=available_ram_bytes,
            free_disk_bytes=free_disk_bytes,
        )
        observed_at = _utc(observed_at or datetime.now(UTC))
        async with self._sessions() as session, session.begin():
            # Serialize event admission across repository instances and runtime restarts.
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                await session.execute(text("SELECT pg_advisory_xact_lock(436043)"))
            elif dialect == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                raise RuntimeError("resource measurement writes require PostgreSQL or SQLite")
            if task_id is not None and await session.get(TaskRecord, task_id) is None:
                raise LookupError(f"Task {task_id} does not exist")
            record = ResourceMeasurementRecord(
                task_id=task_id, observed_at=observed_at, **sample.model_dump()
            )
            session.add(record)
            await session.flush()
            last_event = await session.scalar(
                select(EventRecord.created_at)
                .where(
                    EventRecord.category == "resource",
                    EventRecord.event_type == "resource.measured",
                )
                .order_by(EventRecord.created_at.desc())
                .limit(1)
            )
            if (
                last_event is None
                or observed_at
                >= (last_event.replace(tzinfo=UTC) if last_event.tzinfo is None else last_event)
                + self._event_interval
            ):
                stage_event(
                    session,
                    "resource.measured",
                    source="storage.resources",
                    actor="RUNTIME",
                    aggregate_type="resource_measurement",
                    aggregate_id=record.id,
                    task_id=task_id,
                    payload=sample,
                    created_at=observed_at,
                )
            return record.id

    async def latest(self, *, task_id: UUID | None = None) -> ResourceMeasurementRecord | None:
        rows = await self.range(task_id=task_id, limit=1)
        return rows[0] if rows else None

    async def range(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        task_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResourceMeasurementRecord]:
        """Newest first, with an inclusive start and exclusive end."""
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("limit must be between 1 and 1000 and offset non-negative")
        since = _utc(since) if since is not None else None
        until = _utc(until) if until is not None else None
        if since is not None and until is not None and since >= until:
            raise ValueError("since must precede until")
        statement = select(ResourceMeasurementRecord)
        if since is not None:
            statement = statement.where(ResourceMeasurementRecord.observed_at >= since)
        if until is not None:
            statement = statement.where(ResourceMeasurementRecord.observed_at < until)
        if task_id is not None:
            statement = statement.where(ResourceMeasurementRecord.task_id == task_id)
        statement = (
            statement.order_by(
                ResourceMeasurementRecord.observed_at.desc(), ResourceMeasurementRecord.id
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._sessions() as session:
            return list(await session.scalars(statement))

    async def purge(self, *, retention_days: int, now: datetime | None = None) -> int:
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        cutoff = _utc(now or datetime.now(UTC)) - timedelta(days=retention_days)
        active_task = exists(
            select(TaskRecord.id).where(
                TaskRecord.id == ResourceMeasurementRecord.task_id,
                TaskRecord.state.not_in(_TERMINAL_TASK_STATES),
            )
        )
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                delete(ResourceMeasurementRecord)
                .where(ResourceMeasurementRecord.observed_at < cutoff, ~active_task)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount
