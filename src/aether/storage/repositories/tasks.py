"""Task persistence and the enforced task state machine."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, TaskRecord, TaskStatus, utcnow
from aether.storage.repositories._events import add_event, to_json

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
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

_TERMINAL_STATES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


# Backwards-compatible alias used before the repositories package split.
_ALLOWED_TRANSITIONS = ALLOWED_TASK_TRANSITIONS


class InvalidTaskTransition(ValueError):
    pass


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
        parent_task_id: UUID | None = None,
        requirement_id: UUID | None = None,
        deadline: datetime | None = None,
        interruptible: bool = True,
        estimated_cpu_percent: float | None = None,
        idempotency_key: str | None = None,
        actor: str = "ORCHESTR",
        correlation_id: UUID | None = None,
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
                parent_task_id=parent_task_id,
                requirement_id=requirement_id,
                deadline=deadline,
                interruptible=interruptible,
                estimated_cpu_percent=estimated_cpu_percent,
                idempotency_key=idempotency_key,
            )
            session.add(task)
            await session.flush()
            add_event(
                session,
                event_type="task.created",
                aggregate_type="task",
                aggregate_id=task.id,
                actor=actor,
                payload={
                    "objective_id": objective_id,
                    "title": title,
                    "parent_task_id": parent_task_id,
                    "requirement_id": requirement_id,
                },
                category=EventCategory.TASK,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task.id,
                idempotency_key=f"task.created:{task.id}",
            )
            return task.id

    async def transition(
        self,
        task_id: UUID,
        new_status: TaskStatus,
        *,
        actor: str,
        details: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        new_status = TaskStatus(new_status)
        async with self._sessions() as session, session.begin():
            task = await session.scalar(
                select(TaskRecord).where(TaskRecord.id == task_id).with_for_update()
            )
            if task is None:
                raise LookupError(f"Task {task_id} does not exist")
            old_status = TaskStatus(task.state)
            if new_status not in ALLOWED_TASK_TRANSITIONS[old_status]:
                raise InvalidTaskTransition(
                    f"Cannot transition task from {old_status} to {new_status}"
                )

            now = utcnow()
            task.state = new_status
            task.updated_at = now
            if new_status is TaskStatus.SCHEDULED:
                task.scheduled_at = now
            elif new_status is TaskStatus.RUNNING and task.started_at is None:
                task.started_at = now
            elif new_status in _TERMINAL_STATES:
                task.completed_at = now
            if result is not None:
                task.result = to_json(result)
            if error is not None:
                task.last_error = error
            add_event(
                session,
                event_type="task.transitioned",
                aggregate_type="task",
                aggregate_id=task.id,
                actor=actor,
                payload={
                    "from_status": old_status.value,
                    "to_status": new_status.value,
                    "details": details or {},
                    **({"error": error} if error is not None else {}),
                },
                category=EventCategory.TASK,
                correlation_id=correlation_id,
                objective_id=task.objective_id,
                task_id=task.id,
            )
