"""Deterministic per-agent work queues: which tasks an agent sees is decided in code, not by it.

``WorkQueueService`` answers task-list questions for any agent id; ``AgentWorkQueue`` is the facade
bound to one agent identity that the runtime injects as ``AgentContext.work`` (like ``AgentTools``
and ``AgentModels``). By default an agent sees only its *own open* tasks, ordered by effective
priority (the scheduler's formula) and then age, each with its dependency status precomputed.
Finished tasks are an explicit, bounded opt-in. Reading another agent's task is refused and the
denial is recorded, unless the caller is ORCHESTR (the coordinator) or LEDGER (the record keeper).
No LLM is involved anywhere in this module.
"""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId
from aether.observability.recorder import stage_event
from aether.runtime.scheduler import SchedulerPolicy, effective_priority
from aether.storage.models import TaskDependencyRecord, TaskRecord, TaskStatus

if TYPE_CHECKING:
    from aether.agents.base import AgentContext

OPEN_TASK_STATES: tuple[TaskStatus, ...] = (
    TaskStatus.PENDING,
    TaskStatus.READY,
    TaskStatus.SCHEDULED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_FOR_APPROVAL,
    TaskStatus.WAITING_FOR_VERIFICATION,
    TaskStatus.BLOCKED,
    TaskStatus.PAUSED,
    TaskStatus.RETRYING,
)
FINISHED_TASK_STATES: tuple[TaskStatus, ...] = (
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
)
# May read any agent's task: the coordinator that schedules them and the record keeper.
PRIVILEGED_READERS: frozenset[AgentId] = frozenset({AgentId.ORCHESTR, AgentId.LEDGER})
# May list every agent's tasks that wait for a security decision.
APPROVAL_READERS: frozenset[AgentId] = PRIVILEGED_READERS | {AgentId.WARDEN}
DEFAULT_LIMIT = 50
# Matches the largest configurable per-agent queue (ResearchLimits.max_queue_size).
MAX_LIMIT = 1000
_SOURCE = "agents.work_queue"
_NO_AGGREGATE = UUID(int=0)


class TaskAccessDenied(PermissionError):
    """The caller asked for a task or list it may not see; the denial was recorded."""

    def __init__(self, agent_id: AgentId, operation: str, reason: str) -> None:
        self.agent_id = agent_id
        self.operation = operation
        self.reason = reason
        super().__init__(f"{agent_id.value} may not {operation}: {reason}")


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """One task this task waits for; only identity and progress, never the content."""

    task_id: UUID
    state: TaskStatus | None
    assigned_agent: str | None
    created_at: datetime | None

    @property
    def satisfied(self) -> bool:
        return self.state is TaskStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A task as its agent's queue presents it, with ordering and readiness precomputed."""

    id: UUID
    objective_id: UUID
    title: str
    state: TaskStatus
    assigned_agent: str | None
    priority: int
    effective_priority: float
    created_at: datetime
    updated_at: datetime
    deadline: datetime | None
    completed_at: datetime | None
    attempt: int
    max_attempts: int
    parent_task_id: UUID | None
    requirement_id: UUID | None
    idempotency_key: str | None
    depends_on: tuple[DependencyStatus, ...]
    blocked_by: tuple[UUID, ...]
    ready_to_run: bool

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_TASK_STATES


@dataclass(frozen=True, slots=True)
class TaskDetail(WorkItem):
    """``WorkItem`` plus the task's content; only for its owner or a privileged reader."""

    description: str = ""
    result: dict[str, Any] | None = None
    last_error: str | None = None


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _optional_utc(value: datetime | None) -> datetime | None:
    return as_utc(value) if value is not None else None


def _bounded(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _open_states(states: Iterable[TaskStatus] | None) -> tuple[str, ...]:
    if states is None:
        return tuple(state.value for state in OPEN_TASK_STATES)
    chosen = {TaskStatus(state) for state in states}
    if not chosen or not chosen <= set(OPEN_TASK_STATES):
        raise ValueError("open_tasks only lists open states; use finished_tasks for the rest")
    return tuple(state.value for state in OPEN_TASK_STATES if state in chosen)


def _finished_states(
    states: Iterable[TaskStatus] | None, *, include_cancelled: bool
) -> tuple[str, ...]:
    if states is None:
        chosen = {TaskStatus.COMPLETED, TaskStatus.FAILED}
        if include_cancelled:
            chosen.add(TaskStatus.CANCELLED)
    else:
        chosen = {TaskStatus(state) for state in states}
        if not chosen or not chosen <= set(FINISHED_TASK_STATES):
            raise ValueError("finished_tasks only lists finished states")
    return tuple(state.value for state in FINISHED_TASK_STATES if state in chosen)


def blocked_by(dependencies: Iterable[DependencyStatus]) -> tuple[UUID, ...]:
    """Ids of dependencies that are not completed (missing ones included), in a stable order."""
    return tuple(item.task_id for item in dependencies if not item.satisfied)


def is_ready_to_run(state: TaskStatus, dependencies: Iterable[DependencyStatus]) -> bool:
    """A task can start now: it is ``ready`` and every dependency is completed."""
    return state is TaskStatus.READY and not blocked_by(dependencies)


def queue_order(items: Iterable[WorkItem]) -> list[WorkItem]:
    """Highest effective priority first, then oldest, then id (total, deterministic order)."""
    return sorted(items, key=lambda item: (-item.effective_priority, item.created_at, str(item.id)))


class WorkQueueService:
    """Deterministic task lists for any agent id; agents receive the bound ``AgentWorkQueue``."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy: SchedulerPolicy | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._policy = policy or SchedulerPolicy()

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessions

    def with_sessions(self, sessions: async_sessionmaker[AsyncSession]) -> "WorkQueueService":
        """The same rules over another session factory (e.g. inside an atomic transaction)."""
        if sessions is self._sessions:
            return self
        return WorkQueueService(sessions, clock=self._clock, policy=self._policy)

    # -- lists ---------------------------------------------------------------------------------

    async def open_tasks(
        self,
        agent_id: AgentId,
        *,
        objective_id: UUID | None = None,
        states: Iterable[TaskStatus] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[WorkItem]:
        """``agent_id``'s non-terminal tasks, by effective priority then age; never others'."""
        agent = AgentId(agent_id)
        limit = _bounded(limit)
        statement = select(TaskRecord).where(
            TaskRecord.assigned_agent == agent.value, TaskRecord.state.in_(_open_states(states))
        )
        if objective_id is not None:
            statement = statement.where(TaskRecord.objective_id == objective_id)
        async with self._sessions() as session:
            rows = list(await session.scalars(statement))
            now = as_utc(self._clock())
            ranked = sorted(
                rows,
                key=lambda row: (
                    -self._effective(row, now),
                    as_utc(row.created_at),
                    str(row.id),
                ),
            )[:limit]
            dependencies = await self._dependencies(session, [row.id for row in ranked])
        return [self._item(row, dependencies.get(row.id, ()), now) for row in ranked]

    async def finished_tasks(
        self,
        agent_id: AgentId,
        *,
        since: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        include_cancelled: bool = False,
        objective_id: UUID | None = None,
        states: Iterable[TaskStatus] | None = None,
    ) -> list[WorkItem]:
        """Explicit opt-in: ``agent_id``'s completed/failed (and optionally cancelled) tasks.

        ``states`` narrows the list to a subset of the finished states. Most recently finished
        first, at most ``limit`` (bounded by ``MAX_LIMIT``).
        """
        agent = AgentId(agent_id)
        limit = _bounded(limit)
        finished_at = func.coalesce(TaskRecord.completed_at, TaskRecord.updated_at)
        statement = select(TaskRecord).where(
            TaskRecord.assigned_agent == agent.value,
            TaskRecord.state.in_(_finished_states(states, include_cancelled=include_cancelled)),
        )
        if objective_id is not None:
            statement = statement.where(TaskRecord.objective_id == objective_id)
        if since is not None:
            statement = statement.where(finished_at >= as_utc(since))
        statement = statement.order_by(finished_at.desc(), TaskRecord.id).limit(limit)
        async with self._sessions() as session:
            rows = list(await session.scalars(statement))
            dependencies = await self._dependencies(session, [row.id for row in rows])
        now = as_utc(self._clock())
        return [self._item(row, dependencies.get(row.id, ()), now) for row in rows]

    async def waiting_for_approval(
        self, agent_id: AgentId, *, limit: int = MAX_LIMIT
    ) -> list[WorkItem]:
        """Every agent's tasks waiting for a security decision; WARDEN/ORCHESTR/LEDGER only."""
        agent = AgentId(agent_id)
        limit = _bounded(limit)
        if agent not in APPROVAL_READERS:
            await self._deny(
                agent,
                "list_waiting_for_approval",
                "only WARDEN, ORCHESTR and LEDGER may list other agents' waiting tasks",
            )
        statement = (
            select(TaskRecord)
            .where(TaskRecord.state == TaskStatus.WAITING_FOR_APPROVAL.value)
            .order_by(TaskRecord.updated_at, TaskRecord.id)
            .limit(limit)
        )
        async with self._sessions() as session:
            rows = list(await session.scalars(statement))
            dependencies = await self._dependencies(session, [row.id for row in rows])
        now = as_utc(self._clock())
        return [self._item(row, dependencies.get(row.id, ()), now) for row in rows]

    # -- single tasks --------------------------------------------------------------------------

    async def task_detail(
        self, agent_id: AgentId, task_id: UUID, *, correlation_id: UUID | None = None
    ) -> TaskDetail | None:
        """One task with content; ``None`` if it does not exist.

        Raises ``TaskAccessDenied`` (after recording ``task.access_denied``) when the task
        belongs to another agent and the caller is neither ORCHESTR nor LEDGER.
        """
        agent = AgentId(agent_id)
        async with self._sessions() as session:
            row = await session.get(TaskRecord, task_id)
            if row is None:
                return None
            if self._may_read(agent, row):
                dependencies = await self._dependencies(session, [row.id])
                item = self._item(row, dependencies.get(row.id, ()), as_utc(self._clock()))
                return TaskDetail(
                    **{name: getattr(item, name) for name in WorkItem.__dataclass_fields__},
                    description=row.description,
                    result=row.result,
                    last_error=row.last_error,
                )
            owner, objective = row.assigned_agent, row.objective_id
        await self._deny(
            agent,
            "read_task",
            f"task belongs to {owner or 'no agent'}",
            task_id=task_id,
            objective_id=objective,
            owner=owner,
            correlation_id=correlation_id,
        )

    async def task_by_key(self, agent_id: AgentId, idempotency_key: str) -> WorkItem | None:
        """The caller's own task with ``idempotency_key`` (any state); ``None`` if absent."""
        agent = AgentId(agent_id)
        async with self._sessions() as session:
            row = await session.scalar(
                select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key)
            )
            if row is None:
                return None
            if self._may_read(agent, row):
                dependencies = await self._dependencies(session, [row.id])
                return self._item(row, dependencies.get(row.id, ()), as_utc(self._clock()))
            owner, task_id, objective = row.assigned_agent, row.id, row.objective_id
        await self._deny(
            agent,
            "read_task_by_key",
            f"task belongs to {owner or 'no agent'}",
            task_id=task_id,
            objective_id=objective,
            owner=owner,
        )

    async def incomplete(self, task_ids: Collection[UUID]) -> tuple[UUID, ...]:
        """Which of ``task_ids`` are not completed (missing ones included), in input order.

        Progress only, no content, so any agent may check the tasks it depends on.
        """
        wanted = list(dict.fromkeys(task_ids))
        if not wanted:
            return ()
        async with self._sessions() as session:
            rows = await session.execute(
                select(TaskRecord.id, TaskRecord.state).where(TaskRecord.id.in_(wanted))
            )
            states = {task_id: state for task_id, state in rows}
        return tuple(
            task_id for task_id in wanted if states.get(task_id) != TaskStatus.COMPLETED.value
        )

    # -- internals -----------------------------------------------------------------------------

    @staticmethod
    def _may_read(agent: AgentId, row: TaskRecord) -> bool:
        return agent in PRIVILEGED_READERS or row.assigned_agent == agent.value

    def _effective(self, row: TaskRecord, now: datetime) -> float:
        return effective_priority(
            row.priority,
            waiting_since=as_utc(row.updated_at),
            deadline=_optional_utc(row.deadline),
            now=now,
            policy=self._policy,
        )

    def _item(
        self, row: TaskRecord, dependencies: Iterable[DependencyStatus], now: datetime
    ) -> WorkItem:
        state = TaskStatus(row.state)
        depends_on = tuple(dependencies)
        return WorkItem(
            id=row.id,
            objective_id=row.objective_id,
            title=row.title,
            state=state,
            assigned_agent=row.assigned_agent,
            priority=row.priority,
            effective_priority=self._effective(row, now),
            created_at=as_utc(row.created_at),
            updated_at=as_utc(row.updated_at),
            deadline=_optional_utc(row.deadline),
            completed_at=_optional_utc(row.completed_at),
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            parent_task_id=row.parent_task_id,
            requirement_id=row.requirement_id,
            idempotency_key=row.idempotency_key,
            depends_on=depends_on,
            blocked_by=blocked_by(depends_on),
            ready_to_run=is_ready_to_run(state, depends_on),
        )

    @staticmethod
    async def _dependencies(
        session: AsyncSession, task_ids: list[UUID]
    ) -> dict[UUID, tuple[DependencyStatus, ...]]:
        if not task_ids:
            return {}
        statement = (
            select(
                TaskDependencyRecord.task_id,
                TaskDependencyRecord.depends_on_task_id,
                TaskRecord.state,
                TaskRecord.assigned_agent,
                TaskRecord.created_at,
            )
            .outerjoin(TaskRecord, TaskRecord.id == TaskDependencyRecord.depends_on_task_id)
            .where(TaskDependencyRecord.task_id.in_(task_ids))
        )
        found: dict[UUID, list[DependencyStatus]] = {}
        for task_id, dependency_id, state, agent, created_at in await session.execute(statement):
            found.setdefault(task_id, []).append(
                DependencyStatus(
                    task_id=dependency_id,
                    state=TaskStatus(state) if state is not None else None,
                    assigned_agent=agent,
                    created_at=_optional_utc(created_at),
                )
            )
        epoch = datetime.min.replace(tzinfo=UTC)
        return {
            task_id: tuple(
                sorted(items, key=lambda item: (item.created_at or epoch, str(item.task_id)))
            )
            for task_id, items in found.items()
        }

    async def _deny(
        self,
        agent: AgentId,
        operation: str,
        reason: str,
        *,
        task_id: UUID | None = None,
        objective_id: UUID | None = None,
        owner: str | None = None,
        correlation_id: UUID | None = None,
    ) -> NoReturn:
        async with self._sessions() as session, session.begin():
            stage_event(
                session,
                "task.access_denied",
                source=_SOURCE,
                actor=agent.value,
                aggregate_type="task",
                aggregate_id=task_id or _NO_AGGREGATE,
                payload={
                    "requested_by": agent.value,
                    "operation": operation,
                    "owner": owner,
                    "reason": reason,
                },
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
            )
        raise TaskAccessDenied(agent, operation, reason)


class AgentWorkQueue:
    """The work-queue facade bound to one agent identity; injected as ``AgentContext.work``."""

    __slots__ = ("_service", "agent_id")

    agent_id: AgentId
    _service: WorkQueueService

    def __init__(self, agent_id: AgentId, service: WorkQueueService) -> None:
        object.__setattr__(self, "agent_id", AgentId(agent_id))
        object.__setattr__(self, "_service", service)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AgentWorkQueue is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("AgentWorkQueue is read-only")

    def __repr__(self) -> str:
        return f"AgentWorkQueue(agent_id={self.agent_id.value!r})"

    def rebind(self, sessions: async_sessionmaker[AsyncSession]) -> "AgentWorkQueue":
        """The same identity over another session factory (e.g. an atomic transaction)."""
        if sessions is self._service.sessions:
            return self
        return AgentWorkQueue(self.agent_id, self._service.with_sessions(sessions))

    async def open_tasks(
        self,
        *,
        objective_id: UUID | None = None,
        states: Iterable[TaskStatus] | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[WorkItem]:
        return await self._service.open_tasks(
            self.agent_id, objective_id=objective_id, states=states, limit=limit
        )

    async def finished_tasks(
        self,
        *,
        since: datetime | None = None,
        limit: int = DEFAULT_LIMIT,
        include_cancelled: bool = False,
        objective_id: UUID | None = None,
        states: Iterable[TaskStatus] | None = None,
    ) -> list[WorkItem]:
        return await self._service.finished_tasks(
            self.agent_id,
            since=since,
            limit=limit,
            include_cancelled=include_cancelled,
            objective_id=objective_id,
            states=states,
        )

    async def waiting_for_approval(self, *, limit: int = MAX_LIMIT) -> list[WorkItem]:
        return await self._service.waiting_for_approval(self.agent_id, limit=limit)

    async def task_detail(
        self, task_id: UUID, *, correlation_id: UUID | None = None
    ) -> TaskDetail | None:
        return await self._service.task_detail(
            self.agent_id, task_id, correlation_id=correlation_id
        )

    async def task_by_key(self, idempotency_key: str) -> WorkItem | None:
        return await self._service.task_by_key(self.agent_id, idempotency_key)

    async def incomplete(self, task_ids: Collection[UUID]) -> tuple[UUID, ...]:
        return await self._service.incomplete(task_ids)


def work_queue(ctx: "AgentContext", agent_id: AgentId) -> AgentWorkQueue:
    """``agent_id``'s queue over ``ctx.sessions``: the injected one, or one built from ``ctx``.

    Contexts without an injected queue (tests, the API) get a queue bound to ``agent_id``. An
    injected queue bound to a different agent is a wiring error and is refused.
    """
    agent = AgentId(agent_id)
    bound = ctx.work
    if bound is None:
        return AgentWorkQueue(agent, WorkQueueService(ctx.sessions, clock=ctx.clock))
    if bound.agent_id is not agent:
        raise TaskAccessDenied(
            agent, "use_work_queue", f"the injected queue is bound to {bound.agent_id.value}"
        )
    return bound.rebind(ctx.sessions)


__all__ = [
    "APPROVAL_READERS",
    "DEFAULT_LIMIT",
    "FINISHED_TASK_STATES",
    "MAX_LIMIT",
    "OPEN_TASK_STATES",
    "PRIVILEGED_READERS",
    "AgentWorkQueue",
    "DependencyStatus",
    "TaskAccessDenied",
    "TaskDetail",
    "WorkItem",
    "WorkQueueService",
    "blocked_by",
    "is_ready_to_run",
    "queue_order",
    "work_queue",
]
