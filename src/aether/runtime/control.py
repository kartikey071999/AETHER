"""Durable, cooperative pause/cancel requests for tasks, including RUNNING ones.

A request is a ``decision.recorded`` event on the task aggregate (``task.pause_requested`` /
``task.cancel_requested``); it stays pending until a ``task.control_resolved`` decision lists
its event id as evidence. Tasks that are not running are moved directly with a compare-and-set
update. RUNNING tasks are left to their worker: it signals the agent cooperatively, interrupts
after a grace period, and resolves the request in the same transaction that releases the lease,
so a task that completes first stays completed (the request is resolved as superseded).
"""

import asyncio
import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import stage_event
from aether.runtime._support import Clock, utc_now
from aether.storage.models import EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories import ALLOWED_TASK_TRANSITIONS

TASK_CONTROL_SERVICE = "task_control"
PAUSE_REQUESTED = "task.pause_requested"
CANCEL_REQUESTED = "task.cancel_requested"
CONTROL_RESOLVED = "task.control_resolved"

_SOURCE = "runtime.control"
_DECISION_EVENT = "decision.recorded"
_TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})
_MAX_CAS_ATTEMPTS = 5
_MAX_REASON_CHARS = 500


class ControlAction(enum.StrEnum):
    PAUSE = "pause"
    CANCEL = "cancel"

    @property
    def target(self) -> TaskStatus:
        return TaskStatus.PAUSED if self is ControlAction.PAUSE else TaskStatus.CANCELLED


_REQUEST_DECISIONS = {PAUSE_REQUESTED: ControlAction.PAUSE, CANCEL_REQUESTED: ControlAction.CANCEL}
_DECISION_FOR = {action: decision for decision, action in _REQUEST_DECISIONS.items()}


def strongest(actions: Sequence[ControlAction | None]) -> ControlAction | None:
    """Cancel wins over pause; None when nothing is requested."""
    present = {action for action in actions if action is not None}
    if ControlAction.CANCEL in present:
        return ControlAction.CANCEL
    return ControlAction.PAUSE if present else None


@dataclass(frozen=True, slots=True)
class ControlRequest:
    event_id: UUID
    action: ControlAction


@dataclass(frozen=True, slots=True)
class ControlResult:
    """``applied``: the task moved now; ``pending``: its worker (or next lease) will apply it;
    ``noop``: nothing to do (already terminal or already in the requested state);
    ``resolved``: a worker settled the request concurrently (see the task state)."""

    task_id: UUID
    action: ControlAction
    outcome: str
    state: TaskStatus


class TaskSignals:
    """In-process wake-ups for workers watching RUNNING tasks; holds nothing but events."""

    def __init__(self) -> None:
        self._events: dict[UUID, asyncio.Event] = {}

    def watch(self, task_id: UUID) -> asyncio.Event:
        return self._events.setdefault(task_id, asyncio.Event())

    def unwatch(self, task_id: UUID) -> None:
        self._events.pop(task_id, None)

    def notify(self, task_id: UUID) -> None:
        event = self._events.get(task_id)
        if event is not None:
            event.set()


def _direct_route(state: TaskStatus, action: ControlAction) -> tuple[TaskStatus, ...] | None:
    target = action.target
    if state is target:
        return ()
    if target in ALLOWED_TASK_TRANSITIONS[state]:
        return (target,)
    return None


async def pending_requests(session: AsyncSession, task_id: UUID) -> list[ControlRequest]:
    """Unresolved pause/cancel requests for one task, oldest first."""
    rows = await session.execute(
        select(EventRecord.id, EventRecord.payload)
        .where(
            EventRecord.event_type == _DECISION_EVENT,
            EventRecord.aggregate_type == "task",
            EventRecord.aggregate_id == task_id,
        )
        .order_by(EventRecord.created_at, EventRecord.id)
    )
    requests: list[ControlRequest] = []
    resolved: set[str] = set()
    for event_id, payload in rows.all():
        data = payload if isinstance(payload, dict) else {}
        decision = data.get("decision")
        if decision in _REQUEST_DECISIONS:
            requests.append(ControlRequest(event_id, _REQUEST_DECISIONS[decision]))
        elif decision == CONTROL_RESOLVED:
            resolved.update(str(item) for item in data.get("evidence") or ())
    return [request for request in requests if str(request.event_id) not in resolved]


def stage_resolution(
    session: AsyncSession,
    task_id: UUID,
    objective_id: UUID | None,
    requests: Sequence[ControlRequest],
    outcome: str,
    *,
    actor: str,
    now: datetime,
) -> None:
    """Mark `requests` settled inside the caller's transaction (with the state change)."""
    if not requests:
        return
    stage_event(
        session,
        _DECISION_EVENT,
        source=_SOURCE,
        actor=actor,
        aggregate_type="task",
        aggregate_id=task_id,
        payload={
            "decision": CONTROL_RESOLVED,
            "rationale": outcome,
            "evidence": [str(request.event_id) for request in requests],
        },
        task_id=task_id,
        objective_id=objective_id,
        created_at=now,
    )


class TaskControl:
    """Pause/cancel service exposed to ORCHESTR as ``ctx.services["task_control"]``.

    Holds only the session factory, a clock and in-process wake-up signals, so handing it to an
    agent context exposes no worker, engine or security internals.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = utc_now,
        actor: str = "RUNTIME",
        signals: TaskSignals | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._actor = actor
        self._signals = signals or TaskSignals()

    @property
    def signals(self) -> TaskSignals:
        return self._signals

    def bound_to(self, actor: str) -> "TaskControl":
        """The same control plane, recording requests on behalf of `actor`."""
        return TaskControl(self._sessions, clock=self._clock, actor=actor, signals=self._signals)

    async def request_pause(self, task_id: UUID, *, reason: str = "") -> ControlResult:
        return await self._request(task_id, ControlAction.PAUSE, reason)

    async def request_cancel(self, task_id: UUID, *, reason: str = "") -> ControlResult:
        return await self._request(task_id, ControlAction.CANCEL, reason)

    async def resume(self, task_id: UUID, *, reason: str = "") -> bool:
        """Move a PAUSED task back to READY; False when it is not paused (compare-and-set)."""
        now = self._clock()
        async with self._sessions() as session, session.begin():
            task = await session.get(TaskRecord, task_id, populate_existing=True)
            if task is None:
                raise LookupError(f"Task {task_id} does not exist")
            if TaskStatus(task.state) is not TaskStatus.PAUSED:
                return False
            if not await self._move(
                session, task, TaskStatus.PAUSED, (TaskStatus.READY,), now, reason or "resumed"
            ):
                return False
        return True

    async def pending(self, task_id: UUID) -> list[ControlRequest]:
        async with self._sessions() as session:
            return await pending_requests(session, task_id)

    async def pending_action(self, task_id: UUID) -> ControlAction | None:
        return strongest([request.action for request in await self.pending(task_id)])

    async def settle(self, task_id: UUID) -> ControlResult | None:
        """Apply pending requests to a task that is not RUNNING; None if nothing is pending."""
        for _ in range(_MAX_CAS_ATTEMPTS):
            now = self._clock()
            async with self._sessions() as session, session.begin():
                task = await session.get(TaskRecord, task_id, populate_existing=True)
                if task is None:
                    return None
                requests = await pending_requests(session, task_id)
                action = strongest([request.action for request in requests])
                if action is None:
                    return None
                state = TaskStatus(task.state)
                if state in _TERMINAL:
                    stage_resolution(
                        session,
                        task_id,
                        task.objective_id,
                        requests,
                        f"superseded: task already {state.value}",
                        actor=self._actor,
                        now=now,
                    )
                    return ControlResult(task_id, action, "noop", state)
                if state is TaskStatus.RUNNING:
                    self._signals.notify(task_id)
                    return ControlResult(task_id, action, "pending", state)
                route = _direct_route(state, action)
                if route is None:
                    # e.g. pausing a task waiting for approval: applied when it is next leased.
                    return ControlResult(task_id, action, "pending", state)
                if not await self._move(
                    session, task, state, route, now, f"{action.value} requested"
                ):
                    continue
                stage_resolution(
                    session,
                    task_id,
                    task.objective_id,
                    requests,
                    action.target.value,
                    actor=self._actor,
                    now=now,
                )
                return ControlResult(task_id, action, "applied" if route else "noop", action.target)
        state = await self._state(task_id)
        return ControlResult(task_id, action, "pending", state)

    async def _request(self, task_id: UUID, action: ControlAction, reason: str) -> ControlResult:
        now = self._clock()
        async with self._sessions() as session, session.begin():
            task = await session.get(TaskRecord, task_id, populate_existing=True)
            if task is None:
                raise LookupError(f"Task {task_id} does not exist")
            state = TaskStatus(task.state)
            if state in _TERMINAL:
                return ControlResult(task_id, action, "noop", state)
            stage_event(
                session,
                _DECISION_EVENT,
                source=_SOURCE,
                actor=self._actor,
                aggregate_type="task",
                aggregate_id=task_id,
                payload={
                    "decision": _DECISION_FOR[action],
                    "rationale": (reason or f"{action.value} requested")[:_MAX_REASON_CHARS],
                    "evidence": [],
                },
                task_id=task_id,
                objective_id=task.objective_id,
                created_at=now,
            )
        result = await self.settle(task_id)
        if result is None:  # resolved concurrently, e.g. by a worker releasing the task
            return ControlResult(task_id, action, "resolved", await self._state(task_id))
        return result

    async def _state(self, task_id: UUID) -> TaskStatus:
        async with self._sessions() as session:
            state = await session.scalar(select(TaskRecord.state).where(TaskRecord.id == task_id))
        if state is None:
            raise LookupError(f"Task {task_id} does not exist")
        return TaskStatus(state)

    async def _move(
        self,
        session: AsyncSession,
        task: TaskRecord,
        seen: TaskStatus,
        route: tuple[TaskStatus, ...],
        now: datetime,
        reason: str,
    ) -> bool:
        if not route:
            return True
        result = await session.execute(
            update(TaskRecord)
            .where(TaskRecord.id == task.id, TaskRecord.state == seen.value)
            .values(state=route[-1].value, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        current = seen
        for target in route:
            details: dict[str, Any] = {"reason": reason[:_MAX_REASON_CHARS]}
            stage_event(
                session,
                "task.transitioned",
                source=_SOURCE,
                actor=self._actor,
                aggregate_type="task",
                aggregate_id=task.id,
                payload={
                    "from_status": current.value,
                    "to_status": target.value,
                    "details": details,
                },
                task_id=task.id,
                objective_id=task.objective_id,
                created_at=now,
            )
            current = target
        return True


__all__ = [
    "CANCEL_REQUESTED",
    "CONTROL_RESOLVED",
    "PAUSE_REQUESTED",
    "TASK_CONTROL_SERVICE",
    "ControlAction",
    "ControlRequest",
    "ControlResult",
    "TaskControl",
    "TaskSignals",
    "pending_requests",
    "stage_resolution",
    "strongest",
]
