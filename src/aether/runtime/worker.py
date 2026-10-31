"""Bounded worker pool: concurrency limits, timeouts, lease renewal, and outcome recording.

With a `TaskControl`, workers also honour durable pause/cancel requests for RUNNING tasks: the
agent is signalled cooperatively (`BaseAgent.cancel`), interrupted after a grace period, and the
request is resolved in the same transaction that releases the lease. A result of COMPLETED
always wins over a concurrent request.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.communication.messages import AgentId
from aether.runtime.control import (
    ControlAction,
    ControlRequest,
    TaskControl,
    stage_resolution,
    strongest,
)
from aether.runtime.leases import RUNTIME_ACTOR, Lease, LeaseManager, ReleaseStage
from aether.storage.models import TaskRecord, TaskStatus

_MAX_ERROR_CHARS = 2000

OutcomeCallback = Callable[[UUID, AgentId, TaskStatus], None]

_RESULT_STATUS: dict[TaskResultStatus, tuple[TaskStatus, str]] = {
    TaskResultStatus.COMPLETED: (TaskStatus.COMPLETED, "task.completed"),
    TaskResultStatus.BLOCKED: (TaskStatus.BLOCKED, "task.blocked"),
    TaskResultStatus.WAITING_FOR_APPROVAL: (
        TaskStatus.WAITING_FOR_APPROVAL,
        "task.waiting_for_approval",
    ),
}


@dataclass(frozen=True, slots=True)
class _Disposition:
    status: TaskStatus
    event: str
    details: dict[str, Any]
    refund_attempt: bool = False


@dataclass(slots=True)
class _Running:
    agent: BaseAgent
    lease: Lease
    task: asyncio.Task[None] | None = None
    cancel_reason: str | None = None
    control_action: ControlAction | None = None
    signalled: bool = False


class WorkerPool:
    def __init__(
        self,
        leases: LeaseManager,
        context: AgentContext,
        *,
        max_concurrent: int = 2,
        per_agent_limit: int | Mapping[AgentId, int] | None = None,
        task_timeout: float = 1800.0,
        lease_ttl: timedelta = timedelta(seconds=60),
        renew_interval: float | None = None,
        owner: str = "worker",
        context_for: Callable[[AgentId], AgentContext] | None = None,
        control: TaskControl | None = None,
        control_poll: float = 5.0,
        interrupt_grace: float = 10.0,
        on_outcome: OutcomeCallback | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if control_poll <= 0 or interrupt_grace < 0:
            raise ValueError("control_poll must be positive and interrupt_grace non-negative")
        self._leases = leases
        self._context = context
        self._context_for = context_for or (lambda _agent_id: context)
        self._max_concurrent = max_concurrent
        self._per_agent_limit = per_agent_limit
        self._task_timeout = task_timeout
        self._lease_ttl = lease_ttl
        self._renew_interval = renew_interval or lease_ttl.total_seconds() / 3
        self._owner = owner
        self._control = control
        self._control_poll = control_poll
        self._interrupt_grace = interrupt_grace
        self._on_outcome = on_outcome
        self._global = asyncio.Semaphore(max_concurrent)
        self._agent_semaphores: dict[AgentId, asyncio.Semaphore] = {}
        self._running: dict[UUID, _Running] = {}
        self._reserved: set[UUID] = set()
        self._accepting = True
        self._logger = context.logger

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def running_task_ids(self) -> frozenset[UUID]:
        return frozenset(self._running)

    @property
    def owner(self) -> str:
        return self._owner

    def has_capacity(self, agent_id: AgentId) -> bool:
        return (
            self._accepting
            and not self._global.locked()
            and not self._semaphore_for(agent_id).locked()
        )

    async def submit(self, task_id: UUID, agent: BaseAgent, *, wait: bool = False) -> bool:
        """Lease the task and start it; False when there is no capacity or the lease is lost.

        With `wait=True` the call applies backpressure instead of returning False when full.
        """
        if not self._accepting or task_id in self._running or task_id in self._reserved:
            return False
        agent_semaphore = self._semaphore_for(agent.agent_id)
        if not wait and (self._global.locked() or agent_semaphore.locked()):
            return False
        self._reserved.add(task_id)
        acquired_global = acquired_agent = False
        try:
            await self._global.acquire()
            acquired_global = True
            await agent_semaphore.acquire()
            acquired_agent = True
            owner = f"{self._owner}/{task_id}"
            lease = await self._leases.acquire(task_id, owner, self._lease_ttl)
            if lease is None:
                return False
            if not self._accepting:
                await self._leases.release(
                    task_id,
                    owner,
                    TaskStatus.READY,
                    details={"reason": "shutdown"},
                    refund_attempt=True,
                )
                return False
            running = _Running(agent=agent, lease=lease)
            self._running[task_id] = running
            running.task = asyncio.create_task(
                self._run(task_id, running), name=f"aether-task-{task_id}"
            )
            running.task.add_done_callback(
                lambda _done: self._release_slot(task_id, agent_semaphore)
            )
            acquired_global = acquired_agent = False  # slots are released by the done callback
        finally:
            self._reserved.discard(task_id)
            if acquired_agent:
                agent_semaphore.release()
            if acquired_global:
                self._global.release()
        # Let the worker take its first step so an immediate cancel() always reaches its handler.
        await asyncio.sleep(0)
        return True

    async def cancel(self, task_id: UUID) -> bool:
        running = self._running.get(task_id)
        if running is None or running.task is None:
            return False
        running.cancel_reason = "cancel"
        await running.agent.cancel(task_id)
        running.task.cancel()
        await asyncio.wait({running.task})
        return True

    async def join(self) -> None:
        tasks = [item.task for item in self._running.values() if item.task is not None]
        if tasks:
            await asyncio.wait(tasks)

    async def shutdown(self, grace: float) -> None:
        """Stop accepting work, give running tasks `grace` seconds, then requeue the rest."""
        self._accepting = False
        tasks = [item.task for item in self._running.values() if item.task is not None]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=max(grace, 0.0))
        for running in list(self._running.values()):
            if running.task in pending:
                running.cancel_reason = "shutdown"
                await running.agent.cancel(running.lease.task_id)
                running.task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _semaphore_for(self, agent_id: AgentId) -> asyncio.Semaphore:
        semaphore = self._agent_semaphores.get(agent_id)
        if semaphore is None:
            limit = self._per_agent_limit
            if isinstance(limit, Mapping):
                size = limit.get(agent_id, self._max_concurrent)
            else:
                size = limit or self._max_concurrent
            semaphore = asyncio.Semaphore(max(1, size))
            self._agent_semaphores[agent_id] = semaphore
        return semaphore

    def _release_slot(self, task_id: UUID, agent_semaphore: asyncio.Semaphore) -> None:
        self._running.pop(task_id, None)
        agent_semaphore.release()
        self._global.release()

    async def _run(self, task_id: UUID, running: _Running) -> None:
        lease = running.lease
        control = self._control
        signal = control.signals.watch(task_id) if control is not None else None
        renewer = asyncio.create_task(self._renew_loop(task_id, running))
        watcher = (
            asyncio.create_task(self._control_loop(task_id, running, signal))
            if signal is not None
            else None
        )
        disposition: _Disposition | None = None
        reraise = False
        try:
            running.control_action = await self._requested_action(task_id)
            if running.control_action is not None:
                # Requested before the agent started: do not run it at all.
                disposition = _control_disposition(lease, running.control_action)
            else:
                disposition = await self._execute(task_id, running)
        except asyncio.CancelledError:
            disposition, reraise = self._cancel_disposition(running)
        finally:
            # Stop the watcher before any await so it cannot interrupt a finished task.
            helpers = [task for task in (renewer, watcher) if task is not None]
            for helper in helpers:
                helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            if control is not None:
                control.signals.unwatch(task_id)
        try:
            if disposition is not None:
                await self._finalize(task_id, running, disposition)
        finally:
            if running.signalled:
                running.agent.clear_cancellation(task_id)
        if reraise:
            raise asyncio.CancelledError

    async def _execute(self, task_id: UUID, running: _Running) -> _Disposition:
        lease = running.lease
        async with self._context.sessions() as session:
            task = await session.get(TaskRecord, task_id)
        if task is None:
            return _Disposition(TaskStatus.FAILED, "task.failed", {"error": "task disappeared"})
        timeout = asyncio.timeout(self._task_timeout)
        try:
            async with timeout:
                result = await running.agent.execute_task(
                    task, self._context_for(running.agent.agent_id)
                )
        except TimeoutError as exc:
            if timeout.expired():
                return self._failure(
                    task, lease, "task.timed_out", f"timed out after {self._task_timeout}s", True
                )
            return self._failure(task, lease, "task.failed", _describe(exc), True)
        except Exception as exc:
            self._logger.exception("agent %s failed task %s", running.agent.agent_id, task_id)
            return self._failure(task, lease, "task.failed", _describe(exc), True)
        return self._from_result(task, lease, result)

    def _from_result(self, task: TaskRecord, lease: Lease, result: TaskResult) -> _Disposition:
        if result.status is TaskResultStatus.FAILED:
            return self._failure(
                task,
                lease,
                "task.failed",
                result.error or result.summary or "task reported failure",
                result.retryable,
                result,
            )
        status, event = _RESULT_STATUS[result.status]
        details = {"attempt": lease.attempt, "result": result.model_dump(mode="json")}
        return _Disposition(status, event, details)

    def _failure(
        self,
        task: TaskRecord,
        lease: Lease,
        event: str,
        error: str,
        retryable: bool,
        result: TaskResult | None = None,
    ) -> _Disposition:
        will_retry = retryable and lease.attempt < task.max_attempts
        details: dict[str, Any] = {
            "attempt": lease.attempt,
            "max_attempts": task.max_attempts,
            "error": error[:_MAX_ERROR_CHARS],
            "will_retry": will_retry,
        }
        if result is not None:
            details["result"] = result.model_dump(mode="json")
        return _Disposition(TaskStatus.READY if will_retry else TaskStatus.FAILED, event, details)

    def _cancel_disposition(self, running: _Running) -> tuple[_Disposition | None, bool]:
        attempt = running.lease.attempt
        if running.cancel_reason == "control" and running.control_action is not None:
            return _control_disposition(running.lease, running.control_action), False
        if running.cancel_reason == "cancel":
            disposition = _Disposition(TaskStatus.CANCELLED, "task.cancelled", {"attempt": attempt})
            return disposition, False
        if running.cancel_reason == "lease_lost":
            return None, False
        # Shutdown (or an unexpected cancel): hand the work back without spending an attempt.
        disposition = _Disposition(
            TaskStatus.READY,
            "task.interrupted",
            {"attempt": attempt, "reason": "shutdown"},
            refund_attempt=True,
        )
        return disposition, running.cancel_reason is None

    async def _finalize(self, task_id: UUID, running: _Running, disposition: _Disposition) -> None:
        lease = running.lease
        stage: ReleaseStage | None = None
        if self._control is not None:
            requests = await self._pending(task_id)
            action = strongest([running.control_action, *(item.action for item in requests)])
            if action is not None and disposition.status is not TaskStatus.COMPLETED:
                disposition = _control_disposition(lease, action)
            if requests:
                stage = _resolver(task_id, requests, disposition.status)
        try:
            released = await asyncio.shield(
                self._leases.release(
                    task_id,
                    lease.owner,
                    disposition.status,
                    details=disposition.details,
                    outcome_event=disposition.event,
                    refund_attempt=disposition.refund_attempt,
                    stage=stage,
                )
            )
        except Exception:
            self._logger.exception("failed to record outcome for task %s", task_id)
            return
        if not released:
            self._logger.warning("lease for task %s was lost before recording outcome", task_id)
            return
        if self._control is not None:
            try:
                # Requests that arrived after the final read apply to the released state.
                await self._control.settle(task_id)
            except Exception:
                self._logger.exception("failed to settle control requests for task %s", task_id)
        if self._on_outcome is not None:
            try:
                self._on_outcome(task_id, running.agent.agent_id, disposition.status)
            except Exception:
                self._logger.exception("outcome callback failed for task %s", task_id)

    async def _requested_action(self, task_id: UUID) -> ControlAction | None:
        return strongest([item.action for item in await self._pending(task_id)])

    async def _pending(self, task_id: UUID) -> list[ControlRequest]:
        if self._control is None:
            return []
        try:
            return await self._control.pending(task_id)
        except Exception:
            self._logger.exception("failed to read control requests for task %s", task_id)
            return []

    async def _control_loop(self, task_id: UUID, running: _Running, signal: asyncio.Event) -> None:
        """Watch for pause/cancel requests; signal the agent, then interrupt after the grace."""
        while True:
            try:
                await asyncio.wait_for(signal.wait(), self._control_poll)
            except TimeoutError:
                pass
            signal.clear()
            action = strongest([running.control_action, await self._requested_action(task_id)])
            if action is None:
                continue
            running.control_action = action
            running.signalled = True
            try:
                await running.agent.cancel(task_id)
            except Exception:
                self._logger.exception("agent %s failed to accept a stop", running.agent.agent_id)
            await asyncio.sleep(self._interrupt_grace)
            if running.task is not None and not running.task.done():
                running.cancel_reason = "control"
                running.task.cancel()
            return

    async def _renew_loop(self, task_id: UUID, running: _Running) -> None:
        lease = running.lease
        while True:
            await asyncio.sleep(self._renew_interval)
            try:
                renewed = await self._leases.renew(task_id, lease.owner, self._lease_ttl)
            except Exception:
                self._logger.exception("lease renewal failed for task %s", task_id)
                continue
            if not renewed:
                running.cancel_reason = "lease_lost"
                if running.task is not None:
                    running.task.cancel()
                return


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _control_disposition(lease: Lease, action: ControlAction) -> _Disposition:
    if action is ControlAction.PAUSE:
        # A paused attempt is handed back unspent, like a shutdown interruption.
        return _Disposition(
            TaskStatus.PAUSED,
            "task.interrupted",
            {"attempt": lease.attempt, "reason": "pause_requested"},
            refund_attempt=True,
        )
    return _Disposition(
        TaskStatus.CANCELLED,
        "task.cancelled",
        {"attempt": lease.attempt, "reason": "cancel_requested"},
    )


def _resolver(task_id: UUID, requests: list[ControlRequest], status: TaskStatus) -> ReleaseStage:
    applied = status in (TaskStatus.PAUSED, TaskStatus.CANCELLED)
    outcome = status.value if applied else f"superseded: task {status.value}"

    async def stage(session: AsyncSession, objective_id: UUID | None, now: datetime) -> None:
        stage_resolution(
            session, task_id, objective_id, requests, outcome, actor=RUNTIME_ACTOR, now=now
        )

    return stage


__all__ = ["WorkerPool"]
