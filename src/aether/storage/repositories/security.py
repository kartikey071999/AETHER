"""Security decisions and operation-bound user authorization requests.

Staging methods take the caller's ``AsyncSession`` so the security service can write the
record and its typed audit event (``EventRecorder.add``) in one transaction.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    SecurityDecisionRecord,
)


class SecurityDecisionRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, decision_id: UUID) -> SecurityDecisionRecord | None:
        async with self._sessions() as session:
            return await session.get(SecurityDecisionRecord, decision_id)

    async def list_for_input_hash(self, input_hash: str) -> list[SecurityDecisionRecord]:
        condition = SecurityDecisionRecord.input_hash == input_hash
        statement = select(SecurityDecisionRecord).where(condition)
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(SecurityDecisionRecord.created_at))
            return list(records)

    @staticmethod
    async def stage(
        session: AsyncSession,
        *,
        decision: str,
        operation: str,
        scope: dict[str, Any],
        evidence: list[dict[str, Any]],
        rationale: str,
        input_hash: str,
        expires_at: datetime | None,
        created_at: datetime,
        task_id: UUID | None = None,
    ) -> SecurityDecisionRecord:
        record = SecurityDecisionRecord(
            task_id=task_id,
            decision=decision,
            operation=operation,
            scope=scope,
            evidence=evidence,
            rationale=rationale,
            input_hash=input_hash,
            expires_at=expires_at,
            created_at=created_at,
        )
        session.add(record)
        await session.flush()
        return record

    @staticmethod
    async def latest_for_input_hash(
        session: AsyncSession, input_hash: str
    ) -> SecurityDecisionRecord | None:
        """The most recent decision wins, so a later rejection revokes an earlier approval."""
        statement = (
            select(SecurityDecisionRecord)
            .where(SecurityDecisionRecord.input_hash == input_hash)
            .order_by(SecurityDecisionRecord.created_at.desc(), SecurityDecisionRecord.id)
            .limit(1)
        )
        return await session.scalar(statement)


class OperationAuthorizationRequests:
    """User authorization requests bound to an operation via its WARDEN decision."""

    @staticmethod
    async def latest_for_decision(
        session: AsyncSession, decision_id: UUID
    ) -> AuthorizationRequestRecord | None:
        statement = (
            select(AuthorizationRequestRecord)
            .where(AuthorizationRequestRecord.security_decision_id == decision_id)
            .order_by(AuthorizationRequestRecord.requested_at.desc())
            .limit(1)
        )
        return await session.scalar(statement)

    @staticmethod
    async def stage_pending(
        session: AsyncSession,
        *,
        question: str,
        security_decision_id: UUID,
        requested_at: datetime,
        expires_at: datetime | None,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        input_hash: str | None = None,
    ) -> AuthorizationRequestRecord:
        """Silence never authorizes: the request stays pending until a user answers it."""
        record = AuthorizationRequestRecord(
            question=question,
            input_hash=input_hash,
            objective_id=objective_id,
            task_id=task_id,
            security_decision_id=security_decision_id,
            status=AuthorizationStatus.PENDING,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        session.add(record)
        await session.flush()
        return record
