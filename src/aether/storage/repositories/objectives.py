"""Objective persistence with audited state changes."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, ObjectiveRecord, utcnow
from aether.storage.repositories._events import add_event, to_json


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
                payload={"from_priority": old_priority, "to_priority": priority, "reason": reason},
                category=EventCategory.OBJECTIVE,
                correlation_id=correlation_id,
                objective_id=objective.id,
            )

    async def update_details(
        self,
        objective_id: UUID,
        *,
        description: str,
        completion_criteria: list[str],
        stopping_conditions: list[str],
        deadline: datetime | None,
        review_at: datetime | None,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Replace the editable details; ``None`` explicitly clears a date."""
        for value in (deadline, review_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Objective timestamps must include a timezone")
        changes = {
            "description": description,
            "completion_criteria": list(completion_criteria),
            "stopping_conditions": list(stopping_conditions),
            "deadline": deadline.astimezone(UTC) if deadline is not None else None,
            "review_at": review_at.astimezone(UTC) if review_at is not None else None,
        }
        async with self._sessions() as session, session.begin():
            objective = await _lock_objective(session, objective_id)
            for name, value in changes.items():
                setattr(objective, name, value)
            objective.updated_at = utcnow()
            add_event(
                session,
                event_type="objective.updated",
                aggregate_type="objective",
                aggregate_id=objective.id,
                actor=actor,
                payload={"changes": to_json(changes), "rationale": "Objective details updated"},
                category=EventCategory.OBJECTIVE,
                correlation_id=correlation_id,
                objective_id=objective.id,
            )

    async def set_base_priority(
        self,
        objective_id: UUID,
        value: int,
        *,
        actor: str,
        reason: str,
        correlation_id: UUID | None = None,
    ) -> None:
        """Change the baseline without overwriting the dynamically scheduled priority."""
        if not 0 <= value <= 100:
            raise ValueError("Objective priority must be between 0 and 100")
        async with self._sessions() as session, session.begin():
            objective = await _lock_objective(session, objective_id)
            previous = objective.base_priority
            objective.base_priority = value
            objective.updated_at = utcnow()
            add_event(
                session,
                event_type="objective.base_priority_changed",
                aggregate_type="objective",
                aggregate_id=objective.id,
                actor=actor,
                payload={"from_priority": previous, "to_priority": value, "reason": reason},
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
                payload={"from_status": old_status, "to_status": status, "reason": reason},
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
