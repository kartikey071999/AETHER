"""User authorization requests. Silence is never treated as authorization."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    EventCategory,
    utcnow,
)
from aether.storage.repositories._events import add_event, as_utc


class InvalidAuthorizationResponse(ValueError):
    pass


class AuthorizationRequestRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        question: str,
        actor: str,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        security_decision_id: UUID | None = None,
        expires_at: datetime | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            request = AuthorizationRequestRecord(
                question=question,
                objective_id=objective_id,
                task_id=task_id,
                security_decision_id=security_decision_id,
                status=AuthorizationStatus.PENDING,
                expires_at=expires_at,
            )
            session.add(request)
            await session.flush()
            add_event(
                session,
                event_type="authorization.requested",
                aggregate_type="authorization_request",
                aggregate_id=request.id,
                actor=actor,
                payload={
                    "question": question,
                    "security_decision_id": security_decision_id,
                    "expires_at": expires_at,
                },
                category=EventCategory.AUTHORIZATION,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
                idempotency_key=f"authorization.requested:{request.id}",
            )
            return request.id

    async def respond(
        self,
        request_id: UUID,
        *,
        approved: bool,
        actor: str,
        response: str | None = None,
        now: datetime | None = None,
        correlation_id: UUID | None = None,
    ) -> AuthorizationStatus:
        """Record an explicit answer.

        A response that arrives after ``expires_at`` does not authorize anything; the request is
        marked expired instead and ``AuthorizationStatus.EXPIRED`` is returned.
        """
        current_time = now or utcnow()
        async with self._sessions() as session, session.begin():
            request = await _lock_request(session, request_id)
            if request.status != AuthorizationStatus.PENDING:
                raise InvalidAuthorizationResponse(
                    f"Authorization request {request_id} is already {request.status}"
                )
            expires_at = as_utc(request.expires_at)
            if expires_at is not None and expires_at <= current_time:
                _expire(session, request, actor=actor, correlation_id=correlation_id)
                return AuthorizationStatus.EXPIRED
            status = AuthorizationStatus.APPROVED if approved else AuthorizationStatus.DENIED
            request.status = status
            request.response = response
            request.responded_at = current_time
            add_event(
                session,
                event_type=f"authorization.{status.value}",
                aggregate_type="authorization_request",
                aggregate_id=request.id,
                actor=actor,
                payload={"response": response},
                category=EventCategory.AUTHORIZATION,
                correlation_id=correlation_id,
                objective_id=request.objective_id,
                task_id=request.task_id,
            )
            return status

    async def expire_overdue(
        self,
        *,
        actor: str = "ORCHESTR",
        now: datetime | None = None,
    ) -> list[UUID]:
        """Mark pending requests whose deadline passed as expired, never as approved."""
        current_time = now or utcnow()
        condition = AuthorizationRequestRecord.status == AuthorizationStatus.PENDING
        statement = select(AuthorizationRequestRecord).where(condition)
        expired: list[UUID] = []
        async with self._sessions() as session, session.begin():
            for request in await session.scalars(statement.with_for_update()):
                expires_at = as_utc(request.expires_at)
                if expires_at is not None and expires_at <= current_time:
                    _expire(session, request, actor=actor, correlation_id=None)
                    expired.append(request.id)
        return expired

    async def is_authorized(self, request_id: UUID, *, now: datetime | None = None) -> bool:
        """True only for an explicit, unexpired approval."""
        current_time = now or utcnow()
        async with self._sessions() as session:
            request = await session.get(AuthorizationRequestRecord, request_id)
        if request is None or request.status != AuthorizationStatus.APPROVED:
            return False
        expires_at = as_utc(request.expires_at)
        return expires_at is None or expires_at > current_time


def _expire(
    session: AsyncSession,
    request: AuthorizationRequestRecord,
    *,
    actor: str,
    correlation_id: UUID | None,
) -> None:
    request.status = AuthorizationStatus.EXPIRED
    add_event(
        session,
        event_type="authorization.expired",
        aggregate_type="authorization_request",
        aggregate_id=request.id,
        actor=actor,
        payload={"expires_at": request.expires_at},
        category=EventCategory.AUTHORIZATION,
        correlation_id=correlation_id,
        objective_id=request.objective_id,
        task_id=request.task_id,
    )


async def _lock_request(session: AsyncSession, request_id: UUID) -> AuthorizationRequestRecord:
    condition = AuthorizationRequestRecord.id == request_id
    statement = select(AuthorizationRequestRecord).where(condition)
    request = await session.scalar(statement.with_for_update())
    if request is None:
        raise LookupError(f"Authorization request {request_id} does not exist")
    return request
