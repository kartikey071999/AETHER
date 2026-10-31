"""Database access for ORCHESTR: reads plus audited decision records.

State changes go through the shared repositories; this module only adds queries and the
``decision.recorded`` helper that doubles as ORCHESTR's idempotency marker.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.storage.models import (
    AuthorizationRequestRecord,
    EventRecord,
    ObjectiveRecord,
    RequirementRecord,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import (
    AuthorizationRequestRepository,
    ObjectiveRepository,
    TaskDependencyRepository,
    TaskRepository,
)

SOURCE = "agents.orchestr"
ACTOR = "ORCHESTR"
MAX_KEY_LENGTH = 200

ACTIVE_TASK_STATES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.READY,
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.WAITING_FOR_VERIFICATION,
        TaskStatus.BLOCKED,
        TaskStatus.PAUSED,
        TaskStatus.RETRYING,
    }
)
OCCUPYING_STATES = (TaskStatus.RUNNING, TaskStatus.SCHEDULED, TaskStatus.READY)


@dataclass(frozen=True, slots=True)
class DecisionView:
    key: str | None
    decision: str
    rationale: str
    evidence: tuple[str, ...]
    created_at: datetime
    objective_id: UUID | None
    task_id: UUID | None


def stable_id(*parts: object) -> UUID:
    """Deterministic UUID so republished messages deduplicate on the bus."""
    return uuid5(NAMESPACE_URL, "aether:orchestr:" + ":".join(str(part) for part in parts))


def fit_key(key: str) -> str:
    if len(key) <= MAX_KEY_LENGTH:
        return key
    return key[:120] + ":" + hashlib.sha256(key.encode()).hexdigest()[:32]


class OrchestrStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.objectives = ObjectiveRepository(sessions)
        self.tasks = TaskRepository(sessions)
        self.dependencies = TaskDependencyRepository(sessions)
        self.authorizations = AuthorizationRequestRepository(sessions)
        self._recorder = EventRecorder()

    # --- decisions -------------------------------------------------------------------------

    async def record_decision(
        self,
        decision: str,
        rationale: str,
        *,
        key: str | None = None,
        evidence: Iterable[str] = (),
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> bool:
        """Persist an audit record; with ``key`` it is first-write-wins. True when newly written."""
        aggregate = objective_id or task_id or stable_id("decision", key or decision)
        kind = "objective" if objective_id else "task" if task_id else "decision"
        optional: dict[str, Any] = {
            "objective_id": objective_id,
            "task_id": task_id,
            "correlation_id": correlation_id,
            "idempotency_key": fit_key(key) if key else None,
        }
        event = build_event(
            "decision.recorded",
            source=SOURCE,
            actor=ACTOR,
            aggregate_type=kind,
            aggregate_id=aggregate,
            payload={"decision": decision, "rationale": rationale, "evidence": list(evidence)},
            **{name: value for name, value in optional.items() if value is not None},
        )
        stored = await self._recorder.record(self.sessions, event)
        return stored.id == event.event_id

    async def get_decision(self, key: str) -> DecisionView | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(EventRecord).where(EventRecord.idempotency_key == fit_key(key))
            )
        return _decision_view(row) if row is not None else None

    async def decisions(
        self,
        *,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        decision: str | None = None,
    ) -> list[DecisionView]:
        statement = select(EventRecord).where(EventRecord.event_type == "decision.recorded")
        if objective_id is not None:
            statement = statement.where(EventRecord.objective_id == objective_id)
        if task_id is not None:
            statement = statement.where(EventRecord.task_id == task_id)
        async with self.sessions() as session:
            rows = (await session.scalars(statement.order_by(EventRecord.created_at))).all()
        views = [_decision_view(row) for row in rows]
        return [view for view in views if decision is None or view.decision == decision]

    async def keyed_decisions(self, prefix: str) -> list[DecisionView]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(EventRecord)
                    .where(EventRecord.idempotency_key.like(f"{prefix}%"))
                    .order_by(EventRecord.created_at)
                )
            ).all()
        return [_decision_view(row) for row in rows]

    async def objective_events(
        self, objective_id: UUID, event_type: str
    ) -> list[tuple[datetime, str, dict[str, Any]]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.objective_id == objective_id,
                        EventRecord.event_type == event_type,
                    )
                    .order_by(EventRecord.created_at)
                )
            ).all()
        return [(row.created_at, row.actor, dict(row.payload or {})) for row in rows]

    # --- objectives ------------------------------------------------------------------------

    async def get_objective(self, objective_id: UUID) -> ObjectiveRecord | None:
        async with self.sessions() as session:
            return await session.get(ObjectiveRecord, objective_id)

    async def list_objectives(self, statuses: Sequence[str]) -> list[ObjectiveRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(ObjectiveRecord)
                .where(ObjectiveRecord.status.in_(list(statuses)))
                .order_by(ObjectiveRecord.created_at, ObjectiveRecord.id)
            )
            return list(rows)

    async def count_objectives(self, statuses: Sequence[str]) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ObjectiveRecord)
                    .where(ObjectiveRecord.status.in_(list(statuses)))
                )
                or 0
            )

    async def count_generated_since(self, since: datetime) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ObjectiveRecord)
                    .where(
                        ObjectiveRecord.generated_by_agent.is_(True),
                        ObjectiveRecord.created_at >= since,
                    )
                )
                or 0
            )

    async def count_children(self, objective_id: UUID, statuses: Sequence[str]) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ObjectiveRecord)
                    .where(
                        ObjectiveRecord.parent_objective_id == objective_id,
                        ObjectiveRecord.status.in_(list(statuses)),
                    )
                )
                or 0
            )

    async def find_created_objective(
        self, correlation_id: UUID, title: str
    ) -> ObjectiveRecord | None:
        """The objective an earlier delivery of the same message already created, if any."""
        async with self.sessions() as session:
            ids = (
                await session.scalars(
                    select(EventRecord.objective_id).where(
                        EventRecord.event_type == "objective.created",
                        EventRecord.correlation_id == correlation_id,
                    )
                )
            ).all()
            for objective_id in ids:
                objective = (
                    await session.get(ObjectiveRecord, objective_id) if objective_id else None
                )
                if objective is not None and objective.title == title:
                    return objective
        return None

    # --- tasks -----------------------------------------------------------------------------

    async def get_task(self, task_id: UUID) -> TaskRecord | None:
        async with self.sessions() as session:
            return await session.get(TaskRecord, task_id)

    async def get_requirement(self, requirement_id: UUID) -> RequirementRecord | None:
        async with self.sessions() as session:
            return await session.get(RequirementRecord, requirement_id)

    async def task_by_key(self, idempotency_key: str) -> TaskRecord | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(TaskRecord).where(TaskRecord.idempotency_key == idempotency_key)
            )

    async def tasks_of(
        self, objective_id: UUID, states: Sequence[str] | None = None
    ) -> list[TaskRecord]:
        statement = select(TaskRecord).where(TaskRecord.objective_id == objective_id)
        if states is not None:
            statement = statement.where(TaskRecord.state.in_([str(state) for state in states]))
        async with self.sessions() as session:
            rows = await session.scalars(statement.order_by(TaskRecord.created_at, TaskRecord.id))
            return list(rows)

    async def occupying_tasks(self) -> list[TaskRecord]:
        """Tasks currently holding a scheduling slot (running, scheduled, or released)."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(TaskRecord).where(
                    TaskRecord.state.in_([str(state) for state in OCCUPYING_STATES])
                )
            )
            return list(rows)

    async def dependencies_of(self, task_ids: Iterable[UUID]) -> dict[UUID, set[UUID]]:
        ids = list(task_ids)
        result: dict[UUID, set[UUID]] = {task_id: set() for task_id in ids}
        if not ids:
            return result
        async with self.sessions() as session:
            rows = await session.execute(
                select(TaskDependencyRecord.task_id, TaskDependencyRecord.depends_on_task_id).where(
                    TaskDependencyRecord.task_id.in_(ids)
                )
            )
            for task_id, depends_on in rows:
                result[task_id].add(depends_on)
        return result

    async def states_of(self, task_ids: Iterable[UUID]) -> dict[UUID, str]:
        ids = list(task_ids)
        if not ids:
            return {}
        async with self.sessions() as session:
            rows = await session.execute(
                select(TaskRecord.id, TaskRecord.state).where(TaskRecord.id.in_(ids))
            )
            return {task_id: str(state) for task_id, state in rows}

    async def child_depth(self, task_id: UUID) -> int:
        depth = 0
        current: UUID | None = task_id
        async with self.sessions() as session:
            while current is not None and depth < 64:
                parent = await session.scalar(
                    select(TaskRecord.parent_task_id).where(TaskRecord.id == current)
                )
                if parent is None:
                    break
                depth += 1
                current = parent
        return depth

    async def count_tasks(self, objective_id: UUID) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskRecord)
                    .where(TaskRecord.objective_id == objective_id)
                )
                or 0
            )

    # --- authorization -------------------------------------------------------------------

    async def get_authorization(self, request_id: UUID) -> AuthorizationRequestRecord | None:
        async with self.sessions() as session:
            return await session.get(AuthorizationRequestRecord, request_id)

    async def pending_authorization_for(self, task_id: UUID) -> AuthorizationRequestRecord | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(AuthorizationRequestRecord)
                .where(
                    AuthorizationRequestRecord.task_id == task_id,
                    AuthorizationRequestRecord.status == "pending",
                )
                .order_by(AuthorizationRequestRecord.id)
            )


def _decision_view(row: EventRecord) -> DecisionView:
    payload = dict(row.payload or {})
    return DecisionView(
        key=row.idempotency_key,
        decision=str(payload.get("decision", "")),
        rationale=str(payload.get("rationale", "")),
        evidence=tuple(str(item) for item in payload.get("evidence", []) or []),
        created_at=row.created_at,
        objective_id=row.objective_id,
        task_id=row.task_id,
    )
