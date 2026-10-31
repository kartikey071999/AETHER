"""Shared requirements ledger persistence."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, RequirementRecord, RequirementStatus, utcnow
from aether.storage.repositories._events import add_event

ALLOWED_REQUIREMENT_TRANSITIONS: dict[RequirementStatus, frozenset[RequirementStatus]] = {
    # Direct evaluation and legacy implementation states remain available to existing agents.
    RequirementStatus.PROPOSED: frozenset(
        {
            RequirementStatus.EVALUATING,
            RequirementStatus.APPROVED,
            RequirementStatus.REJECTED,
            RequirementStatus.SUPERSEDED,
        }
    ),
    RequirementStatus.EVALUATING: frozenset(
        {RequirementStatus.APPROVED, RequirementStatus.REJECTED, RequirementStatus.NEEDS_EVIDENCE}
    ),
    RequirementStatus.NEEDS_EVIDENCE: frozenset({RequirementStatus.EVALUATING}),
    RequirementStatus.APPROVED: frozenset(
        {
            RequirementStatus.IMPLEMENTED,
            RequirementStatus.IN_PROGRESS,
            RequirementStatus.SATISFIED,
            RequirementStatus.REJECTED,
            RequirementStatus.SUPERSEDED,
        }
    ),
    RequirementStatus.IMPLEMENTED: frozenset(
        {RequirementStatus.VERIFIED, RequirementStatus.FAILED}
    ),
    RequirementStatus.IN_PROGRESS: frozenset(
        {
            RequirementStatus.IMPLEMENTED,
            RequirementStatus.SATISFIED,
            RequirementStatus.FAILED,
            RequirementStatus.SUPERSEDED,
        }
    ),
    RequirementStatus.SATISFIED: frozenset({RequirementStatus.IN_PROGRESS}),
    RequirementStatus.REJECTED: frozenset(),
    RequirementStatus.SUPERSEDED: frozenset(),
    RequirementStatus.VERIFIED: frozenset(),
    RequirementStatus.FAILED: frozenset(),
}


class InvalidRequirementTransition(ValueError):
    pass


class RequirementConflictError(ValueError):
    """A caller-chosen ID already identifies different requirement content."""


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
        requirement_id: UUID | None = None,
    ) -> UUID:
        content = {
            "objective_id": objective_id,
            "task_id": task_id,
            "title": title,
            "description": description,
            "requirement_type": requirement_type,
            "acceptance_criteria": list(acceptance_criteria or []),
            "source_finding_id": source_finding_id,
            "priority": priority,
            "security_review_required": security_review_required,
            "created_by": created_by,
        }
        try:
            async with self._sessions() as session, session.begin():
                if requirement_id is not None:
                    existing = await session.get(RequirementRecord, requirement_id)
                    if existing is not None:
                        return _match_content(existing, content)
                requirement = RequirementRecord(
                    id=requirement_id or uuid4(),
                    status=RequirementStatus.PROPOSED,
                    **content,
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
        except IntegrityError:
            # A concurrent creator may have committed the same ID after our lookup.
            if requirement_id is not None:
                async with self._sessions() as session:
                    existing = await session.get(RequirementRecord, requirement_id)
                    if existing is not None:
                        return _match_content(existing, content)
            raise

    async def set_status(
        self,
        requirement_id: UUID,
        status: RequirementStatus,
        *,
        actor: str,
        reason: str | None = None,
        correlation_id: UUID | None = None,
        reopen: bool = False,
    ) -> None:
        status = RequirementStatus(status)
        async with self._sessions() as session, session.begin():
            statement = select(RequirementRecord).where(RequirementRecord.id == requirement_id)
            requirement = await session.scalar(statement.with_for_update())
            if requirement is None:
                raise LookupError(f"Requirement {requirement_id} does not exist")
            old_status = RequirementStatus(requirement.status)
            if reopen:
                if not (
                    old_status is RequirementStatus.REJECTED
                    and status is RequirementStatus.PROPOSED
                    and actor == "SCOUT"
                    and reason is not None
                    and reason.strip()
                ):
                    raise InvalidRequirementTransition(
                        "Only SCOUT may explicitly reopen a rejected requirement with a reason"
                    )
            elif status not in ALLOWED_REQUIREMENT_TRANSITIONS[old_status]:
                raise InvalidRequirementTransition(
                    f"Cannot transition requirement from {old_status} to {status}"
                )
            requirement.status = status
            requirement.updated_at = utcnow()
            add_event(
                session,
                event_type="requirement.status_changed",
                aggregate_type="requirement",
                aggregate_id=requirement.id,
                actor=actor,
                payload={"from_status": old_status, "to_status": status, "reason": reason},
                category=EventCategory.REQUIREMENT,
                correlation_id=correlation_id,
                objective_id=requirement.objective_id,
                task_id=requirement.task_id,
            )

    async def reopen(
        self,
        requirement_id: UUID,
        *,
        actor: str,
        reason: str,
        correlation_id: UUID | None = None,
    ) -> None:
        await self.set_status(
            requirement_id,
            RequirementStatus.PROPOSED,
            actor=actor,
            reason=reason,
            correlation_id=correlation_id,
            reopen=True,
        )

    async def list_by_status(self, status: RequirementStatus) -> list[RequirementRecord]:
        condition = RequirementRecord.status == RequirementStatus(status)
        statement = select(RequirementRecord).where(condition)
        ordering = (RequirementRecord.priority.desc(), RequirementRecord.created_at)
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(*ordering))
            return list(records)


def _match_content(requirement: RequirementRecord, content: dict[str, Any]) -> UUID:
    if any(getattr(requirement, name) != value for name, value in content.items()):
        raise RequirementConflictError(
            f"Requirement {requirement.id} already exists with different content"
        )
    return requirement.id
