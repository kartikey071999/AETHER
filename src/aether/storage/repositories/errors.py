"""Error records and the recovery attempts made against them."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import ErrorRecord, EventCategory, RecoveryAttemptRecord
from aether.storage.repositories._events import add_event


class ErrorRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        *,
        error_type: str,
        message: str,
        agent: str | None = None,
        aggregate_type: str | None = None,
        aggregate_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            error = ErrorRecord(
                error_type=error_type,
                message=message,
                agent=agent,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                details=details or {},
            )
            session.add(error)
            await session.flush()
            add_event(
                session,
                event_type="error.recorded",
                aggregate_type="error",
                aggregate_id=error.id,
                actor=agent or "SYSTEM",
                payload={
                    "error_type": error_type,
                    "message": message,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                },
                category=EventCategory.ERROR,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
            )
            return error.id

    async def record_recovery_attempt(
        self,
        error_id: UUID,
        *,
        action: str,
        outcome: str,
        actor: str,
        details: dict[str, Any] | None = None,
        resolves: bool = False,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Record a recovery attempt; ``resolves=True`` also marks the error resolved."""
        async with self._sessions() as session, session.begin():
            error = await _lock_error(session, error_id)
            attempt = RecoveryAttemptRecord(
                error_id=error.id,
                action=action,
                outcome=outcome,
                details=details or {},
            )
            session.add(attempt)
            if resolves:
                error.resolved = True
            await session.flush()
            add_event(
                session,
                event_type="error.recovery_attempted",
                aggregate_type="error",
                aggregate_id=error.id,
                actor=actor,
                payload={
                    "recovery_attempt_id": attempt.id,
                    "action": action,
                    "outcome": outcome,
                    "resolved": error.resolved,
                },
                category=EventCategory.ERROR,
                correlation_id=correlation_id,
            )
            return attempt.id

    async def resolve(
        self,
        error_id: UUID,
        *,
        actor: str,
        note: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            error = await _lock_error(session, error_id)
            error.resolved = True
            add_event(
                session,
                event_type="error.resolved",
                aggregate_type="error",
                aggregate_id=error.id,
                actor=actor,
                payload={"note": note},
                category=EventCategory.ERROR,
                correlation_id=correlation_id,
            )

    async def list_unresolved(self) -> list[ErrorRecord]:
        statement = select(ErrorRecord).where(ErrorRecord.resolved.is_(False))
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(ErrorRecord.occurred_at))
            return list(records)

    async def list_recovery_attempts(self, error_id: UUID) -> list[RecoveryAttemptRecord]:
        condition = RecoveryAttemptRecord.error_id == error_id
        statement = select(RecoveryAttemptRecord).where(condition)
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(RecoveryAttemptRecord.attempted_at))
            return list(records)


async def _lock_error(session: AsyncSession, error_id: UUID) -> ErrorRecord:
    statement = select(ErrorRecord).where(ErrorRecord.id == error_id)
    error = await session.scalar(statement.with_for_update())
    if error is None:
        raise LookupError(f"Error record {error_id} does not exist")
    return error
