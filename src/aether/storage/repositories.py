"""Transactional operations that keep durable state and audit events aligned."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventRecord, ObjectiveRecord, TaskRecord, TaskStatus

_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
    TaskStatus.READY: frozenset(
        {TaskStatus.SCHEDULED, TaskStatus.BLOCKED, TaskStatus.PAUSED, TaskStatus.CANCELLED}
    ),
    TaskStatus.SCHEDULED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.RETRYING,
            TaskStatus.BLOCKED,
            TaskStatus.PAUSED,
            TaskStatus.WAITING_FOR_APPROVAL,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.BLOCKED: frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
    TaskStatus.RETRYING: frozenset(
        {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.FAILED: frozenset({TaskStatus.RETRYING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransition(ValueError):
    pass


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
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            objective = ObjectiveRecord(
                title=title,
                description=description,
                priority=priority,
                created_by=created_by,
                rationale=rationale,
                completion_criteria=completion_criteria or [],
            )
            session.add(objective)
            await session.flush()
            session.add(
                EventRecord(
                    event_type="objective.created",
                    aggregate_type="objective",
                    aggregate_id=objective.id,
                    actor=created_by,
                    payload={"title": title, "priority": priority},
                    idempotency_key=f"objective.created:{objective.id}",
                )
            )
            return objective.id


class TaskRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        objective_id: UUID,
        title: str,
        description: str = "",
        assigned_agent: str | None = None,
        priority: int = 50,
        estimated_ram_mb: int = 0,
        estimated_disk_mb: int = 0,
        max_attempts: int = 3,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            task = TaskRecord(
                objective_id=objective_id,
                title=title,
                description=description,
                assigned_agent=assigned_agent,
                priority=priority,
                estimated_ram_mb=estimated_ram_mb,
                estimated_disk_mb=estimated_disk_mb,
                max_attempts=max_attempts,
            )
            session.add(task)
            await session.flush()
            session.add(
                EventRecord(
                    event_type="task.created",
                    aggregate_type="task",
                    aggregate_id=task.id,
                    actor="ORCHESTR",
                    payload={"objective_id": str(objective_id), "title": title},
                    idempotency_key=f"task.created:{task.id}",
                )
            )
            return task.id

    async def transition(
        self,
        task_id: UUID,
        new_status: TaskStatus,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            task = await session.scalar(
                select(TaskRecord).where(TaskRecord.id == task_id).with_for_update()
            )
            if task is None:
                raise LookupError(f"Task {task_id} does not exist")
            old_status = TaskStatus(task.state)
            if new_status not in _ALLOWED_TRANSITIONS[old_status]:
                raise InvalidTaskTransition(
                    f"Cannot transition task from {old_status} to {new_status}"
                )

            task.state = new_status
            task.updated_at = datetime.now(UTC)
            session.add(
                EventRecord(
                    event_type="task.transitioned",
                    aggregate_type="task",
                    aggregate_id=task.id,
                    actor=actor,
                    payload={
                        "from": old_status.value,
                        "to": new_status.value,
                        "details": details or {},
                    },
                )
            )


async def get_objective(session: AsyncSession, objective_id: UUID) -> ObjectiveRecord | None:
    return await session.get(ObjectiveRecord, objective_id)
