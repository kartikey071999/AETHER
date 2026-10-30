"""Exclusive, expiring task leases backed by compare-and-set updates."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import TaskOutcomePayload
from aether.runtime._support import Clock, add_event, as_utc, utc_now
from aether.storage.models import TaskRecord, TaskStatus
from aether.storage.repositories import ALLOWED_TASK_TRANSITIONS, InvalidTaskTransition

RUNTIME_ACTOR = "RUNTIME"
_SOURCE = "runtime.leases"
_OUTCOME_FIELDS = frozenset(TaskOutcomePayload.model_fields) - {"details"}


def _outcome_payload(details: dict[str, Any]) -> dict[str, Any]:
    """Split worker details into the typed outcome fields plus a free-form remainder."""
    payload = {name: value for name, value in details.items() if name in _OUTCOME_FIELDS}
    payload["details"] = {name: v for name, v in details.items() if name not in _OUTCOME_FIELDS}
    return payload


# Terminal-ish targets that need several hops to respect the allowed transition graph.
_ROUTES: dict[TaskStatus, tuple[TaskStatus, ...]] = {
    TaskStatus.READY: (TaskStatus.RETRYING, TaskStatus.READY),
    TaskStatus.CANCELLED: (TaskStatus.FAILED, TaskStatus.CANCELLED),
}


@dataclass(frozen=True, slots=True)
class Lease:
    task_id: UUID
    owner: str
    expires_at: datetime
    attempt: int


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    task_id: UUID
    previous_owner: str | None
    attempt: int
    max_attempts: int
    new_status: TaskStatus


def _matches(column: Any, value: Any) -> Any:
    """Equality that also matches NULL, for compare-and-set on nullable lease columns."""
    return column.is_(None) if value is None else column == value


class LeaseManager:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = utc_now,
        actor: str = RUNTIME_ACTOR,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._actor = actor

    async def acquire(self, task_id: UUID, owner: str, ttl: timedelta) -> Lease | None:
        """Lease a READY/SCHEDULED task and move it to RUNNING; None if it cannot be leased."""
        now = self._clock()
        expires_at = now + ttl
        async with self._sessions() as session, session.begin():
            task = await session.get(TaskRecord, task_id, populate_existing=True)
            if task is None:
                raise LookupError(f"Task {task_id} does not exist")
            seen_state = TaskStatus(task.state)
            if seen_state not in (TaskStatus.READY, TaskStatus.SCHEDULED):
                return None
            if task.attempt >= task.max_attempts:
                return None
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.state == seen_state.value,
                    TaskRecord.attempt == task.attempt,
                    or_(
                        TaskRecord.lease_owner.is_(None),
                        TaskRecord.lease_expires_at.is_(None),
                        TaskRecord.lease_expires_at <= now,
                    ),
                )
                .values(
                    state=TaskStatus.RUNNING.value,
                    attempt=task.attempt + 1,
                    lease_owner=owner,
                    lease_expires_at=expires_at,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            attempt = task.attempt + 1
            hops = [TaskStatus.RUNNING]
            if seen_state is TaskStatus.READY:
                hops.insert(0, TaskStatus.SCHEDULED)
            self._stage_transitions(
                session,
                task_id,
                task.objective_id,
                seen_state,
                hops,
                owner,
                now,
                {"attempt": attempt},
            )
            add_event(
                session,
                event_type="task.lease_acquired",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=self._actor,
                payload={"owner": owner, "attempt": attempt, "expires_at": expires_at},
                created_at=now,
                source=_SOURCE,
                task_id=task_id,
                objective_id=task.objective_id,
            )
        return Lease(task_id=task_id, owner=owner, expires_at=expires_at, attempt=attempt)

    async def renew(self, task_id: UUID, owner: str, ttl: timedelta) -> bool:
        """Extend a live lease; False if the lease was lost or already expired."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.state == TaskStatus.RUNNING.value,
                    TaskRecord.lease_owner == owner,
                    TaskRecord.lease_expires_at > now,
                )
                .values(lease_expires_at=now + ttl, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            return result.rowcount == 1

    async def release(
        self,
        task_id: UUID,
        owner: str,
        final_status: TaskStatus,
        *,
        details: dict[str, Any] | None = None,
        outcome_event: str | None = None,
        refund_attempt: bool = False,
    ) -> bool:
        """Release a lease held by `owner`, moving the task to `final_status`.

        READY and CANCELLED are reached through the intermediate states the transition graph
        requires. Returns False when the caller no longer owns a RUNNING lease.
        """
        now = self._clock()
        route = _ROUTES.get(final_status, (final_status,))
        self._validate_route(TaskStatus.RUNNING, route)
        async with self._sessions() as session, session.begin():
            values: dict[str, Any] = {
                "state": route[-1].value,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": now,
            }
            if refund_attempt:
                values["attempt"] = TaskRecord.attempt - 1
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.state == TaskStatus.RUNNING.value,
                    TaskRecord.lease_owner == owner,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return False
            objective_id = await session.scalar(
                select(TaskRecord.objective_id).where(TaskRecord.id == task_id)
            )
            self._stage_transitions(
                session,
                task_id,
                objective_id,
                TaskStatus.RUNNING,
                list(route),
                owner,
                now,
                details or {},
            )
            add_event(
                session,
                event_type="task.lease_released",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=self._actor,
                payload={"owner": owner, "final_status": route[-1].value},
                created_at=now,
                source=_SOURCE,
                task_id=task_id,
                objective_id=objective_id,
            )
            if outcome_event:
                add_event(
                    session,
                    event_type=outcome_event,
                    aggregate_type="task",
                    aggregate_id=task_id,
                    actor=owner[:24],
                    payload=_outcome_payload(details or {}),
                    created_at=now,
                    source=_SOURCE,
                    task_id=task_id,
                    objective_id=objective_id,
                )
        return True

    async def owned_by(self, owner_prefix: str) -> list[tuple[UUID, str]]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(TaskRecord.id, TaskRecord.lease_owner).where(
                    TaskRecord.state == TaskStatus.RUNNING.value,
                    TaskRecord.lease_owner.startswith(owner_prefix),
                )
            )
            return [(task_id, owner) for task_id, owner in rows.all() if owner]

    async def recover_expired(self, now: datetime | None = None) -> list[RecoveryOutcome]:
        """Requeue (or fail, once attempts are exhausted) RUNNING tasks whose lease expired."""
        now = as_utc(now) if now else self._clock()
        async with self._sessions() as session:
            rows = await session.execute(
                select(TaskRecord.id, TaskRecord.lease_owner, TaskRecord.lease_expires_at).where(
                    TaskRecord.state == TaskStatus.RUNNING.value,
                    or_(
                        TaskRecord.lease_expires_at.is_(None),
                        TaskRecord.lease_expires_at <= now,
                    ),
                )
            )
            stale = rows.all()
        outcomes: list[RecoveryOutcome] = []
        for task_id, seen_owner, seen_expiry in stale:
            outcome = await self._recover_one(task_id, seen_owner, seen_expiry, now)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    async def _recover_one(
        self,
        task_id: UUID,
        seen_owner: str | None,
        seen_expiry: datetime | None,
        now: datetime,
    ) -> RecoveryOutcome | None:
        async with self._sessions() as session, session.begin():
            task = await session.get(TaskRecord, task_id, populate_existing=True)
            if task is None:
                return None
            retry = task.attempt < task.max_attempts
            final = TaskStatus.READY if retry else TaskStatus.FAILED
            result = await session.execute(
                update(TaskRecord)
                .where(
                    TaskRecord.id == task_id,
                    TaskRecord.state == TaskStatus.RUNNING.value,
                    _matches(TaskRecord.lease_owner, seen_owner),
                    _matches(TaskRecord.lease_expires_at, seen_expiry),
                )
                .values(state=final.value, lease_owner=None, lease_expires_at=None, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            add_event(
                session,
                event_type="task.lease_expired",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=self._actor,
                payload={
                    "owner": seen_owner,
                    "expired_at": as_utc(seen_expiry) if seen_expiry else None,
                },
                created_at=now,
                source=_SOURCE,
                task_id=task_id,
                objective_id=task.objective_id,
            )
            details = {"reason": "lease_expired", "attempt": task.attempt}
            self._stage_transitions(
                session,
                task_id,
                task.objective_id,
                TaskStatus.RUNNING,
                [TaskStatus.RETRYING, final],
                self._actor,
                now,
                details,
            )
            add_event(
                session,
                event_type="recovery.attempted",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=self._actor,
                payload={
                    "operation": "lease_expiry",
                    "attempt": task.attempt,
                    "success": retry,
                    "reason": "requeued" if retry else "failed: attempts exhausted",
                    "max_attempts": task.max_attempts,
                    "previous_owner": seen_owner,
                },
                created_at=now,
                source=_SOURCE,
                task_id=task_id,
                objective_id=task.objective_id,
            )
            return RecoveryOutcome(
                task_id=task_id,
                previous_owner=seen_owner,
                attempt=task.attempt,
                max_attempts=task.max_attempts,
                new_status=final,
            )

    @staticmethod
    def _validate_route(start: TaskStatus, route: tuple[TaskStatus, ...]) -> None:
        current = start
        for target in route:
            if target not in ALLOWED_TASK_TRANSITIONS[current]:
                raise InvalidTaskTransition(f"Cannot transition task from {current} to {target}")
            current = target

    def _stage_transitions(
        self,
        session: AsyncSession,
        task_id: UUID,
        objective_id: UUID | None,
        start: TaskStatus,
        hops: list[TaskStatus],
        owner: str,
        now: datetime,
        details: dict[str, Any],
    ) -> None:
        current = start
        for target in hops:
            add_event(
                session,
                event_type="task.transitioned",
                aggregate_type="task",
                aggregate_id=task_id,
                actor=self._actor,
                payload={
                    "from_status": current.value,
                    "to_status": target.value,
                    "details": {"owner": owner, **details},
                },
                created_at=now,
                source=_SOURCE,
                task_id=task_id,
                objective_id=objective_id,
            )
            current = target
