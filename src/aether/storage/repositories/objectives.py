"""Objective persistence with audited state changes."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, ObjectiveRecord, utcnow
from aether.storage.repositories._events import add_event


class ObjectiveRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        title: str,
        description: str = "",
        priority: int = 50,
        created_by: str = "ORCHESTR",
        rationale: str | None = None,
        completion_criteria: list[str] | None = None,
        parent_objective_id: UUID | None = None,
        deadline: datetime | None = None,
        review_at: datetime | None = None,
        expected_benefit: str | None = None,
        estimated_cost: float | None = None,
        stopping_conditions: list[str] | None = None,
        generated_by_agent: bool = False,
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            objective = ObjectiveRecord(
                title=title,
                description=description,
                priority=priority,
                base_priority=priority,
                created_by=created_by,
                rationale=rationale,
                completion_criteria=completion_criteria or [],
                parent_objective_id=parent_objective_id,
                deadline=deadline,
                review_at=review_at,
                expected_benefit=expected_benefit,
                estimated_cost=estimated_cost,
                stopping_conditions=stopping_conditions or [],
                generated_by_agent=generated_by_agent,
            )
            session.add(objective)
            await session.flush()
            add_event(
                session,
                event_type="objective.created",
                aggregate_type="objective",
                aggregate_id=objective.id,
                actor=created_by,
                payload={
                    "title": title,
                    "priority": priority,
                    "parent_objective_id": parent_objective_id,
                    "generated_by_agent": generated_by_agent,
                },
                category=EventCategory.OBJECTIVE,
                correlation_id=correlation_id,
                objective_id=objective.id,
                idempotency_key=f"objective.created:{objective.id}",
            )
            return objective.id

    async def update_priority(
        self,
        objective_id: UUID,
        priority: int,
        *,
        actor: str,
        reason: str,
        correlation_id: UUID | None = None,
    ) -> None:
        if not 0 <= priority <= 100:
            raise ValueError("Objective priority must be between 0 and 100")
        async with self._sessions() as session, session.begin():
            objective = await _lock_objective(session, objective_id)
            old_priority = objective.priority
            now = utcnow()
            objective.priority = priority
            objective.last_priority_change_at = now
            objective.updated_at = now
            add_event(
                session,
                event_type="objective.priority_changed",
                aggregate_type="objective",
                aggregate_id=objective.id,
                actor=actor,
                payload={"from": old_priority, "to": priority, "reason": reason},
                category=EventCategory.OBJECTIVE,
                correlation_id=correlation_id,
                objective_id=objective.id,
            )

    async def set_status(
        self,
        objective_id: UUID,
        status: str,
        *,
        actor: str,
        reason: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            objective = await _lock_objective(session, objective_id)
            old_status = objective.status
            objective.status = status
            objective.updated_at = utcnow()
            add_event(
                session,
                event_type="objective.status_changed",
                aggregate_type="objective",
                aggregate_id=objective.id,
                actor=actor,
                payload={"from": old_status, "to": status, "reason": reason},
                category=EventCategory.OBJECTIVE,
                correlation_id=correlation_id,
                objective_id=objective.id,
            )


async def _lock_objective(session: AsyncSession, objective_id: UUID) -> ObjectiveRecord:
    objective = await session.scalar(
        select(ObjectiveRecord).where(ObjectiveRecord.id == objective_id).with_for_update()
    )
    if objective is None:
        raise LookupError(f"Objective {objective_id} does not exist")
    return objective


async def get_objective(session: AsyncSession, objective_id: UUID) -> ObjectiveRecord | None:
    return await session.get(ObjectiveRecord, objective_id)
