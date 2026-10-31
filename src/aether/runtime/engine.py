"""Shared runtime engine: agent loops, scheduler tick, worker pool, and graceful lifecycle."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.base import (
    AgentContext,
    AgentHealth,
    BaseAgent,
    HealthStatus,
    PermissionService,
    UnsupportedMessageType,
)
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage
from aether.config import AetherSettings
from aether.observability.events import build_event
from aether.observability.logging import log_event
from aether.observability.recorder import EventRecorder
from aether.runtime._support import Clock, as_utc, utc_now
from aether.runtime.control import TASK_CONTROL_SERVICE, TaskControl
from aether.runtime.leases import RUNTIME_ACTOR, LeaseManager
from aether.runtime.lifecycle import Lifecycle, LifecycleState
from aether.runtime.models import ModelServices
from aether.runtime.recovery import RecoveryReport, recover_runtime_state
from aether.runtime.resource_monitor import HostResources
from aether.runtime.scheduler import (
    Scheduler,
    SchedulerResources,
    SchedulingCandidate,
)
from aether.runtime.security import WARDEN_ONLY_SERVICES, SecurityServices
from aether.runtime.shutdown import ShutdownSignalHandler
from aether.runtime.worker import WorkerPool
from aether.storage.models import TaskRecord, TaskStatus
from aether.storage.repositories import ExecutionRunRepository, ResourceMeasurementRepository

_MIB = 1024 * 1024
_MAX_ERROR_CHARS = 2000


class ResourceSnapshotter(Protocol):
    def snapshot(self, disk_path: Path) -> HostResources: ...


class EngineLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_concurrent_tasks: int = Field(default=2, ge=1)
    max_tasks_per_agent: int | None = Field(default=None, ge=1)
    task_timeout_seconds: float = Field(default=1800.0, gt=0)
    lease_ttl_seconds: float = Field(default=60.0, gt=0)
    lease_renew_seconds: float | None = Field(default=None, gt=0)
    scheduler_interval_seconds: float = Field(default=1.0, gt=0)
    recovery_interval_seconds: float = Field(default=15.0, gt=0)
    message_poll_seconds: float = Field(default=1.0, gt=0)
    message_batch_size: int = Field(default=10, ge=1)
    shutdown_timeout_seconds: float = Field(default=30.0, ge=0)
    disk_path: Path = Path(".")
    # Periodic agent hooks: interval measured with the engine clock, checked every poll.
    agent_tick_seconds: float = Field(default=30.0, gt=0)
    agent_tick_overrides: dict[AgentId, float] = Field(default_factory=dict)
    agent_tick_timeout_seconds: float = Field(default=120.0, gt=0)
    tick_poll_seconds: float = Field(default=1.0, gt=0)
    # Cooperative pause/cancel of running tasks.
    task_control_poll_seconds: float = Field(default=5.0, gt=0)
    task_interrupt_grace_seconds: float = Field(default=10.0, ge=0)
    resource_sample_seconds: float = Field(default=30.0, ge=1, allow_inf_nan=False)
    resource_retention_days: int = Field(default=30, ge=1)
    resource_event_seconds: float = Field(default=300.0, ge=60, allow_inf_nan=False)

    @classmethod
    def from_settings(cls, settings: AetherSettings) -> "EngineLimits":
        return cls(
            max_concurrent_tasks=settings.max_concurrent_tasks,
            task_timeout_seconds=settings.task_timeout_seconds,
            disk_path=settings.data_dir,
            agent_tick_seconds=settings.agent_tick_seconds,
            agent_tick_overrides=settings.agent_tick_overrides,
            agent_tick_timeout_seconds=settings.agent_tick_timeout_seconds,
            task_control_poll_seconds=settings.task_control_poll_seconds,
            task_interrupt_grace_seconds=settings.task_interrupt_grace_seconds,
            resource_sample_seconds=settings.resource_sample_seconds,
            resource_retention_days=settings.resource_retention_days,
            resource_event_seconds=settings.resource_event_seconds,
        )

    def tick_seconds_for(self, agent_id: AgentId) -> float:
        return self.agent_tick_overrides.get(agent_id, self.agent_tick_seconds)


TickHook = Callable[[AgentContext], Awaitable[Any]]

# Periodic hooks agents already expose under their own names (adapted, not renamed).
TICK_HOOK_ALIASES = ("resume_waiting_tasks", "resume_pending")


def tick_hook(agent: BaseAgent) -> tuple[str, TickHook] | None:
    """The agent's periodic hook: an overridden `tick`, else a known alias; None if neither."""
    if getattr(type(agent), "tick", BaseAgent.tick) is not BaseAgent.tick:
        return "tick", agent.tick
    for name in TICK_HOOK_ALIASES:
        hook = getattr(agent, name, None)
        if callable(hook):
            return name, hook
    return None


class Engine:
    def __init__(
        self,
        database_sessions: async_sessionmaker[AsyncSession],
        agents: Sequence[BaseAgent],
        scheduler: Scheduler,
        resource_monitor: ResourceSnapshotter,
        limits: EngineLimits | None = None,
        *,
        bus: MessageBus | None = None,
        clock: Clock = utc_now,
        logger: logging.Logger | None = None,
        permission_service: PermissionService | None = None,
        security: SecurityServices | None = None,
        models: ModelServices | None = None,
        agent_services: Mapping[AgentId, Mapping[str, Any]] | None = None,
        task_control: TaskControl | None = None,
    ) -> None:
        self._sessions = database_sessions
        self._agents: dict[AgentId, BaseAgent] = {}
        for agent in agents:
            if agent.agent_id in self._agents:
                raise ValueError(f"Duplicate agent registered: {agent.agent_id.value}")
            self._agents[agent.agent_id] = agent
        self._scheduler = scheduler
        self._monitor = resource_monitor
        self._limits = limits or EngineLimits()
        self._clock = clock
        self._logger = logger or logging.getLogger("aether.runtime.engine")
        self._run_id = uuid4()
        self._owner = f"engine-{self._run_id.hex[:8]}"
        self._bus = bus or MessageBus(
            database_sessions, clock=clock, poll_interval=self._limits.message_poll_seconds
        )
        self._leases = LeaseManager(database_sessions, clock=clock)
        self._context = AgentContext(
            sessions=database_sessions,
            bus=self._bus,
            clock=clock,
            logger=self._logger,
            permission_service=permission_service,
        )
        lease_ttl = timedelta(seconds=self._limits.lease_ttl_seconds)
        self._security = security
        self._models = models
        self._contexts = (
            security.contexts_for(self._agents, self._context) if security is not None else {}
        )
        if models is not None:
            self._contexts = {
                agent_id: models.context_for(agent_id, self._contexts.get(agent_id, self._context))
                for agent_id in self._agents
            }
        self._task_control = task_control or TaskControl(database_sessions, clock=clock)
        self._resources = ResourceMeasurementRepository(
            database_sessions, event_interval_seconds=self._limits.resource_event_seconds
        )
        self._resource_lock = asyncio.Lock()
        self._last_resource_sample: datetime | None = None
        self._last_resource_purge: datetime | None = None
        self._inject_services(agent_services or {})
        self._tick_hooks = {
            agent_id: hook
            for agent_id, agent in self._agents.items()
            if (hook := tick_hook(agent)) is not None
        }
        self._tick_wakes = {agent_id: asyncio.Event() for agent_id in self._tick_hooks}
        self._agent_locks = {agent_id: asyncio.Lock() for agent_id in self._agents}
        self._pool = WorkerPool(
            self._leases,
            self._context,
            context_for=self.context_for,
            max_concurrent=self._limits.max_concurrent_tasks,
            per_agent_limit=self._limits.max_tasks_per_agent,
            task_timeout=self._limits.task_timeout_seconds,
            lease_ttl=lease_ttl,
            renew_interval=self._limits.lease_renew_seconds,
            owner=self._owner,
            control=self._task_control,
            control_poll=self._limits.task_control_poll_seconds,
            interrupt_grace=self._limits.task_interrupt_grace_seconds,
            on_outcome=self._on_task_outcome,
        )
        self._lifecycle = Lifecycle()
        self._loops: list[asyncio.Task[None]] = []
        self._stop_requested = asyncio.Event()
        self._stopped = asyncio.Event()
        self._loops_stopping = asyncio.Event()
        self.recovery_report: RecoveryReport | None = None
        # Extra typed fields (e.g. spool replay counts) merged into the runtime.started event.
        self.startup_details: dict[str, Any] = {}

    @property
    def state(self) -> LifecycleState:
        return self._lifecycle.state

    @property
    def bus(self) -> MessageBus:
        return self._bus

    @property
    def leases(self) -> LeaseManager:
        return self._leases

    @property
    def pool(self) -> WorkerPool:
        return self._pool

    @property
    def run_id(self) -> UUID:
        return self._run_id

    @property
    def security(self) -> SecurityServices | None:
        return self._security

    @property
    def models(self) -> ModelServices | None:
        return self._models

    @property
    def task_control(self) -> TaskControl:
        """Durable pause/cancel requests; ORCHESTR gets the same plane as a service."""
        return self._task_control

    @property
    def tick_hooks(self) -> dict[AgentId, str]:
        """The periodic hook name the engine calls for each agent that has one."""
        return {agent_id: name for agent_id, (name, _) in self._tick_hooks.items()}

    def agent(self, agent_id: AgentId) -> BaseAgent:
        return self._agents[agent_id]

    def context_for(self, agent_id: AgentId) -> AgentContext:
        """The injected context for one agent: shared services, WARDEN-only approvals."""
        return self._contexts.get(agent_id, self._context)

    def request_tick(self, agent_id: AgentId) -> None:
        """Run `agent_id`'s periodic hook as soon as possible (no-op if it has none)."""
        wake = self._tick_wakes.get(agent_id)
        if wake is not None:
            wake.set()

    async def run_agent_tick(self, agent_id: AgentId) -> Any:
        """Run one bounded, exception-safe tick; failures are recorded and return None."""
        hook = self._tick_hooks.get(agent_id)
        if hook is None:
            return None
        name, call = hook
        try:
            async with self._agent_locks[agent_id]:
                async with asyncio.timeout(self._limits.agent_tick_timeout_seconds):
                    return await call(self.context_for(agent_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.exception("%s tick hook %s failed", agent_id, name)
            await self._record_tick_error(agent_id, name, exc)
            return None

    def _inject_services(self, agent_services: Mapping[AgentId, Mapping[str, Any]]) -> None:
        unknown = set(agent_services) - set(self._agents)
        if unknown:
            raise ValueError(f"services for unregistered agents: {sorted(map(str, unknown))}")
        for agent_id in self._agents:
            extra = dict(agent_services.get(agent_id, {}))
            if agent_id is AgentId.ORCHESTR:
                extra.setdefault("resource_measurements", self._resources)
                extra.setdefault("execution_runs", ExecutionRunRepository(self._sessions))
                extra.setdefault(
                    TASK_CONTROL_SERVICE, self._task_control.bound_to(AgentId.ORCHESTR.value)
                )
            if agent_id is not AgentId.WARDEN and set(extra) & set(WARDEN_ONLY_SERVICES):
                raise ValueError(f"{agent_id.value} must not receive WARDEN-only services")
            if not extra:
                continue
            base = self._contexts.get(agent_id, self._context)
            self._contexts[agent_id] = replace(base, services={**base.services, **extra})

    def _on_task_outcome(self, task_id: UUID, agent_id: AgentId, status: TaskStatus) -> None:
        if status is TaskStatus.WAITING_FOR_APPROVAL:
            # Apply WARDEN decisions recorded while the task was still running.
            self.request_tick(AgentId.WARDEN)
            if agent_id is AgentId.LEDGER:
                # The test run it waits for may have finished before the task started waiting.
                self.request_tick(AgentId.LEDGER)

    async def start(self) -> None:
        self._lifecycle.transition(LifecycleState.STARTING)
        try:
            self.recovery_report = await recover_runtime_state(
                self._leases, self._bus, list(self._agents)
            )
            await self.sample_resources_once()
            for agent in self._agents.values():
                await agent.start()
            if self._security is not None:
                await self._emit(
                    "runtime.security_initialized", self._security.initialized_payload()
                )
            if self._models is not None:
                await self._emit(
                    "runtime.models_initialized", await self._models.initialized_payload()
                )
            for agent in self._agents.values():
                self._spawn(self._agent_loop(agent), f"agent-loop-{agent.agent_id.value}")
            for agent_id in self._tick_hooks:
                self._spawn(self._tick_loop(agent_id), f"agent-tick-{agent_id.value}")
            self._spawn(self._scheduler_loop(), "scheduler-loop")
            self._spawn(self._recovery_loop(), "recovery-loop")
            self._spawn(self._resource_loop(), "resource-loop")
            self._lifecycle.transition(LifecycleState.RUNNING)
            await self._emit(
                "runtime.started",
                {
                    "agents": [agent_id.value for agent_id in self._agents],
                    "owner": self._owner,
                    "recovery": self.recovery_report.summary(),
                    **self.startup_details,
                },
            )
        except BaseException:
            await self._teardown(0.0)
            raise

    async def stop(self, timeout: float | None = None) -> None:
        self._stop_requested.set()
        if self._lifecycle.state is LifecycleState.CREATED:
            self._lifecycle.transition(LifecycleState.STOPPED)
            self._stopped.set()
            return
        if self._lifecycle.state in (LifecycleState.STOPPING, LifecycleState.STOPPED):
            await self._stopped.wait()
            return
        grace = self._limits.shutdown_timeout_seconds if timeout is None else timeout
        self._lifecycle.transition(LifecycleState.STOPPING)
        await self._emit("runtime.stopping", {"running_tasks": self._pool.running_count})
        released = await self._teardown(grace)
        await self._emit("runtime.stopped", {"released_leases": released})

    async def run_forever(self) -> None:
        if self._lifecycle.state is LifecycleState.CREATED:
            await self.start()
        handler = ShutdownSignalHandler(self.request_stop)
        handler.install(asyncio.get_running_loop())
        try:
            await self._stop_requested.wait()
        finally:
            handler.uninstall()
            await self.stop()

    def request_stop(self) -> None:
        """Ask run_forever() to shut down; must be called on the event loop thread."""
        self._stop_requested.set()

    async def health(self) -> dict[AgentId, AgentHealth]:
        report: dict[AgentId, AgentHealth] = {}
        for agent_id, agent in self._agents.items():
            try:
                report[agent_id] = await agent.health_check()
            except Exception as exc:  # noqa: BLE001 - a broken agent must not break health
                report[agent_id] = AgentHealth(
                    agent_id=agent_id, status=HealthStatus.UNHEALTHY, detail=str(exc)
                )
        return report

    async def schedule_once(self) -> int:
        """Run one scheduler tick; returns the number of tasks started."""
        if not self._lifecycle.accepting_work:
            return 0
        statement = (
            select(TaskRecord)
            .where(
                TaskRecord.state == TaskStatus.READY.value,
                TaskRecord.assigned_agent.in_([agent_id.value for agent_id in self._agents]),
            )
            .order_by(TaskRecord.priority.desc(), TaskRecord.created_at, TaskRecord.id)
        )
        async with self._sessions() as session:
            rows = (await session.scalars(statement)).all()
        candidates = {
            row.id: (
                AgentId(row.assigned_agent),
                SchedulingCandidate(
                    task_id=row.id,
                    priority=row.priority,
                    waiting_since=as_utc(row.updated_at),
                    estimated_ram_bytes=row.estimated_ram_mb * _MIB,
                    estimated_disk_bytes=row.estimated_disk_mb * _MIB,
                ),
            )
            for row in rows
            if row.assigned_agent
        }
        started = 0
        while candidates:
            eligible = [
                candidate
                for agent_id, candidate in candidates.values()
                if self._pool.has_capacity(agent_id)
            ]
            if not eligible:
                break
            decision = self._scheduler.choose_next(
                eligible,
                SchedulerResources(
                    host=self._monitor.snapshot(self._limits.disk_path),
                    running_tasks=self._pool.running_count,
                ),
                now=self._clock(),
            )
            if decision.selected_task_id is None:
                break
            try:
                await self._scheduler.record_decision(decision)
            except RuntimeError:
                pass  # scheduler built without a session factory
            agent_id, _ = candidates.pop(decision.selected_task_id)
            if await self._pool.submit(decision.selected_task_id, self._agents[agent_id]):
                started += 1
        return started

    async def sample_resources_once(self) -> bool:
        """Persist at most one sample per interval, independent of scheduler activity."""
        async with self._resource_lock:
            now = self._clock()
            if (
                self._last_resource_sample is not None
                and now
                < self._last_resource_sample
                + timedelta(seconds=self._limits.resource_sample_seconds)
            ):
                return False
            if self._last_resource_purge is None or now >= self._last_resource_purge + timedelta(
                days=1
            ):
                await self._resources.purge(
                    retention_days=self._limits.resource_retention_days, now=now
                )
                self._last_resource_purge = now
            host = self._monitor.snapshot(self._limits.disk_path)
            await self._resources.record(
                cpu_percent=host.cpu_percent,
                available_ram_bytes=host.available_ram_bytes,
                free_disk_bytes=host.free_disk_bytes,
                observed_at=now,
            )
            self._last_resource_sample = now
            return True

    def _spawn(self, coroutine: Any, name: str) -> None:
        self._loops.append(asyncio.create_task(coroutine, name=name))

    async def _stop_loops(self, grace: float) -> None:
        """Let loops finish their current iteration; cancelling mid-query can wedge SQLite."""
        loops, self._loops = self._loops, []
        self._loops_stopping.set()
        if loops:
            _, pending = await asyncio.wait(loops, timeout=grace)
            for loop_task in pending:
                loop_task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._loops_stopping.wait(), seconds)
        except TimeoutError:
            pass

    async def _teardown(self, grace: float) -> int:
        await self._stop_loops(grace)
        await self._pool.shutdown(grace)
        released = 0
        for task_id, owner in await self._leases.owned_by(self._owner):
            if await self._leases.release(
                task_id,
                owner,
                TaskStatus.READY,
                details={"reason": "shutdown"},
                outcome_event="task.interrupted",
                refund_attempt=True,
            ):
                released += 1
        for agent in self._agents.values():
            try:
                await agent.stop()
            except Exception:
                self._logger.exception("agent %s failed to stop cleanly", agent.agent_id)
        if self._lifecycle.state is not LifecycleState.STOPPING:
            self._lifecycle.transition(LifecycleState.STOPPING)
        self._lifecycle.transition(LifecycleState.STOPPED)
        self._stopped.set()
        return released

    async def _agent_loop(self, agent: BaseAgent) -> None:
        while not self._loops_stopping.is_set():
            try:
                if not await self._bus.wait_for_messages(
                    agent.agent_id, self._limits.message_poll_seconds
                ):
                    continue
                messages = await self._bus.claim(
                    agent.agent_id, self._limits.message_batch_size, consumer=self._owner
                )
                for message in messages:
                    await self._process_message(agent, message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("message loop for %s failed", agent.agent_id)
                await self._pause(min(self._limits.message_poll_seconds, 1.0))

    async def _process_message(self, agent: BaseAgent, message: AgentMessage) -> None:
        await self._bus.mark_processing(message.message_id, consumer=self._owner)
        try:
            async with self._agent_locks[agent.agent_id]:
                await agent.dispatch_message(message, self.context_for(agent.agent_id))
        except UnsupportedMessageType as exc:
            await self._bus.mark_failed(message.message_id, json.dumps(exc.to_dict()))
        except Exception as exc:
            self._logger.exception("%s failed to handle %s", agent.agent_id, message.message_id)
            await self._bus.mark_failed(message.message_id, f"{type(exc).__name__}: {exc}")
        else:
            await self._bus.mark_processed(message.message_id)

    async def _tick_loop(self, agent_id: AgentId) -> None:
        """Tick once on start, then every interval or when woken; never dies on a failure."""
        interval = timedelta(seconds=self._limits.tick_seconds_for(agent_id))
        wake = self._tick_wakes[agent_id]
        wake.clear()
        await self.run_agent_tick(agent_id)
        next_due = self._clock() + interval
        while not self._loops_stopping.is_set():
            if wake.is_set() or self._clock() >= next_due:
                wake.clear()
                await self.run_agent_tick(agent_id)
                next_due = self._clock() + interval
                continue
            remaining = (next_due - self._clock()).total_seconds()
            await self._wait_for_wake(wake, min(self._limits.tick_poll_seconds, remaining))

    async def _wait_for_wake(self, wake: asyncio.Event, seconds: float) -> None:
        waiters = [
            asyncio.ensure_future(wake.wait()),
            asyncio.ensure_future(self._loops_stopping.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=max(seconds, 0.0), return_when="FIRST_COMPLETED")
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def _record_tick_error(self, agent_id: AgentId, hook: str, exc: Exception) -> None:
        try:
            await self._emit(
                "error.raised",
                {
                    "error_type": type(exc).__name__,
                    "message": (str(exc) or type(exc).__name__)[:_MAX_ERROR_CHARS],
                    "details": {"agent_id": agent_id.value, "hook": hook},
                },
            )
        except Exception:
            self._logger.exception("failed to record tick failure for %s", agent_id)

    async def _scheduler_loop(self) -> None:
        while not self._loops_stopping.is_set():
            try:
                await self.schedule_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("scheduler tick failed")
            await self._pause(self._limits.scheduler_interval_seconds)

    async def _recovery_loop(self) -> None:
        while True:
            await self._pause(self._limits.recovery_interval_seconds)
            if self._loops_stopping.is_set():
                return
            try:
                await self._leases.recover_expired()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("lease recovery sweep failed")

    async def _resource_loop(self) -> None:
        while True:
            await self._pause(self._limits.resource_sample_seconds)
            if self._loops_stopping.is_set():
                return
            try:
                await self.sample_resources_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("resource sampling or retention failed")

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        event = build_event(
            event_type,
            source="runtime.engine",
            actor=RUNTIME_ACTOR,
            aggregate_type="runtime",
            aggregate_id=self._run_id,
            payload=payload,
            timestamp=self._clock(),
        )
        async with self._sessions() as session, session.begin():
            EventRecorder().add(session, event)
        log_event(event)
