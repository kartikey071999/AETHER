"""Task dependency edges with cycle detection."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, TaskDependencyRecord, TaskRecord
from aether.storage.repositories._events import add_event


class DependencyCycleError(ValueError):
    pass


class TaskDependencyRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(
        self,
        task_id: UUID,
        depends_on_task_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> bool:
        """Add ``task_id -> depends_on_task_id``; returns False if the edge already exists."""
        if task_id == depends_on_task_id:
            raise DependencyCycleError("A task cannot depend on itself")
        async with self._sessions() as session, session.begin():
            ids = (task_id, depends_on_task_id)
            statement = select(TaskRecord).where(TaskRecord.id.in_(ids)).with_for_update()
            tasks = {task.id: task for task in await session.scalars(statement)}
            missing = [str(item) for item in ids if item not in tasks]
            if missing:
                raise LookupError(f"Tasks do not exist: {', '.join(missing)}")
            if await session.get(TaskDependencyRecord, ids) is not None:
                return False
            if await _reaches(session, start=depends_on_task_id, target=task_id):
                raise DependencyCycleError(
                    f"Dependency {task_id} -> {depends_on_task_id} would create a cycle"
                )
            edge = TaskDependencyRecord(task_id=task_id, depends_on_task_id=depends_on_task_id)
            session.add(edge)
            add_event(
                session,
                event_type="task.dependency_added",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=actor,
                payload={"depends_on_task_id": depends_on_task_id},
                category=EventCategory.TASK,
                correlation_id=correlation_id,
                objective_id=tasks[task_id].objective_id,
                task_id=task_id,
            )
            return True

    async def remove(
        self,
        task_id: UUID,
        depends_on_task_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> bool:
        async with self._sessions() as session, session.begin():
            edge = await session.get(TaskDependencyRecord, (task_id, depends_on_task_id))
            if edge is None:
                return False
            await session.delete(edge)
            add_event(
                session,
                event_type="task.dependency_removed",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=actor,
                payload={"depends_on_task_id": depends_on_task_id},
                category=EventCategory.TASK,
                correlation_id=correlation_id,
                task_id=task_id,
            )
            return True

    async def list_dependencies(self, task_id: UUID) -> list[UUID]:
        """Tasks that ``task_id`` waits for."""
        column = TaskDependencyRecord.depends_on_task_id
        statement = select(column).where(TaskDependencyRecord.task_id == task_id)
        async with self._sessions() as session:
            return list(await session.scalars(statement))

    async def list_dependents(self, task_id: UUID) -> list[UUID]:
        """Tasks waiting for ``task_id``."""
        column = TaskDependencyRecord.task_id
        statement = select(column).where(TaskDependencyRecord.depends_on_task_id == task_id)
        async with self._sessions() as session:
            return list(await session.scalars(statement))


async def _reaches(session: AsyncSession, *, start: UUID, target: UUID) -> bool:
    """Breadth-first search along existing dependency edges from ``start``."""
    seen: set[UUID] = {start}
    frontier: set[UUID] = {start}
    while frontier:
        if target in frontier:
            return True
        column = TaskDependencyRecord.depends_on_task_id
        statement = select(column).where(TaskDependencyRecord.task_id.in_(frontier))
        next_ids = set(await session.scalars(statement)) - seen
        seen |= next_ids
        frontier = next_ids
    return False
