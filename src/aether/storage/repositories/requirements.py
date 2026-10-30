"""Shared requirements ledger persistence."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, RequirementRecord, RequirementStatus, utcnow
from aether.storage.repositories._events import add_event


class RequirementRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        objective_id: UUID,
        title: str,
        requirement_type: str,
        created_by: str,
        description: str = "",
        acceptance_criteria: list[str] | None = None,
        task_id: UUID | None = None,
        source_finding_id: UUID | None = None,
        priority: int = 50,
        security_review_required: bool = False,
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            requirement = RequirementRecord(
                objective_id=objective_id,
                task_id=task_id,
                title=title,
                description=description,
                requirement_type=requirement_type,
                status=RequirementStatus.PROPOSED,
                acceptance_criteria=acceptance_criteria or [],
                source_finding_id=source_finding_id,
                priority=priority,
                security_review_required=security_review_required,
                created_by=created_by,
            )
            session.add(requirement)
            await session.flush()
            add_event(
                session,
                event_type="requirement.created",
                aggregate_type="requirement",
                aggregate_id=requirement.id,
                actor=created_by,
                payload={
                    "title": title,
                    "requirement_type": requirement_type,
                    "source_finding_id": source_finding_id,
                    "security_review_required": security_review_required,
                },
                category=EventCategory.REQUIREMENT,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
                idempotency_key=f"requirement.created:{requirement.id}",
            )
            return requirement.id

    async def set_status(
        self,
        requirement_id: UUID,
        status: RequirementStatus,
        *,
        actor: str,
        reason: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        status = RequirementStatus(status)
        async with self._sessions() as session, session.begin():
            statement = select(RequirementRecord).where(RequirementRecord.id == requirement_id)
            requirement = await session.scalar(statement.with_for_update())
            if requirement is None:
                raise LookupError(f"Requirement {requirement_id} does not exist")
            old_status = requirement.status
            requirement.status = status
            requirement.updated_at = utcnow()
            add_event(
                session,
                event_type="requirement.status_changed",
                aggregate_type="requirement",
                aggregate_id=requirement.id,
                actor=actor,
                payload={"from": old_status, "to": status, "reason": reason},
                category=EventCategory.REQUIREMENT,
                correlation_id=correlation_id,
                objective_id=requirement.objective_id,
                task_id=requirement.task_id,
            )

    async def list_by_status(self, status: RequirementStatus) -> list[RequirementRecord]:
        condition = RequirementRecord.status == RequirementStatus(status)
        statement = select(RequirementRecord).where(condition)
        ordering = (RequirementRecord.priority.desc(), RequirementRecord.created_at)
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(*ordering))
            return list(records)
