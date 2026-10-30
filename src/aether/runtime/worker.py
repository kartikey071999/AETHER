"""Bounded worker pool: concurrency limits, timeouts, lease renewal, and outcome recording."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.communication.messages import AgentId
from aether.runtime.leases import Lease, LeaseManager
from aether.storage.models import TaskRecord, TaskStatus

_MAX_ERROR_CHARS = 2000

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
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._leases = leases
        self._context = context
        self._max_concurrent = max_concurrent
        self._per_agent_limit = per_agent_limit
        self._task_timeout = task_timeout
        self._lease_ttl = lease_ttl
        self._renew_interval = renew_interval or lease_ttl.total_seconds() / 3
        self._owner = owner
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
        renewer = asyncio.create_task(self._renew_loop(task_id, running))
        disposition: _Disposition | None = None
        reraise = False
        try:
            disposition = await self._execute(task_id, running)
        except asyncio.CancelledError:
            disposition, reraise = self._cancel_disposition(running)
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        if disposition is not None:
            await self._finalize(task_id, lease, disposition)
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
                result = await running.agent.execute_task(task, self._context)
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

    async def _finalize(self, task_id: UUID, lease: Lease, disposition: _Disposition) -> None:
        try:
            released = await asyncio.shield(
                self._leases.release(
                    task_id,
                    lease.owner,
                    disposition.status,
                    details=disposition.details,
                    outcome_event=disposition.event,
                    refund_attempt=disposition.refund_attempt,
                )
            )
        except Exception:
            self._logger.exception("failed to record outcome for task %s", task_id)
            return
        if not released:
            self._logger.warning("lease for task %s was lost before recording outcome", task_id)

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


__all__ = ["WorkerPool"]
