"""Runtime agent hooks: per-agent services, periodic ticks, WARDEN wake-ups, pause/cancel."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents import AGENT_DEFINITIONS
from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.muse import MuseAgent, MuseSettings
from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.scout.schemas import ResearchLimits
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.config import AetherSettings
from aether.runtime.bootstrap import Runtime, build_runtime
from aether.runtime.control import CONTROL_RESOLVED, TaskControl
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.lifecycle import LifecycleState
from aether.runtime.resource_monitor import HostResources, ResourceMonitor
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.runtime.settings import AgentRuntimeSettings
from aether.security import ApprovalService, OperationKind, SecuritySettings
from aether.security.policy import SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import Base, EventRecord, TaskRecord, TaskStatus
from aether.storage.repositories import ObjectiveRepository

ALL_AGENTS = [definition.agent_id for definition in AGENT_DEFINITIONS]
ORCHESTR_KEYS = {"resource_monitor", "scheduler_policy", "disk_path", "orchestr_limits"}
PRIVILEGED_KEYS = {"approvals", "security_policy"}


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeMonitor:
    def snapshot(self, disk_path: Path) -> HostResources:
        return HostResources(
            cpu_percent=1.0, available_ram_bytes=16 * 1024**3, free_disk_bytes=500 * 1024**3
        )


Behaviour = Callable[[BaseAgent, TaskRecord], Awaitable[TaskResult]]


async def _complete(_agent: BaseAgent, _task: TaskRecord) -> TaskResult:
    return TaskResult(status=TaskResultStatus.COMPLETED, summary="ok")


class WorkAgent(BaseAgent):
    """FORGE stand-in whose task behaviour is supplied by the test."""

    agent_id = AgentId.FORGE
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self, behaviour: Behaviour = _complete) -> None:
        super().__init__()
        self.behaviour = behaviour
        self.executed: list[UUID] = []
        self.started = asyncio.Event()

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        self.executed.append(task.id)
        self.started.set()
        return await self.behaviour(self, task)


class TickingAgent(BaseAgent):
    """Overrides the BaseAgent `tick` hook; can fail or hang on chosen calls."""

    agent_id = AgentId.ORCHESTR
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self, *, fail_on: frozenset[int] = frozenset(), hang_on: int | None = None):
        super().__init__()
        self.calls: list[datetime] = []
        self.fail_on = fail_on
        self.hang_on = hang_on

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        return TaskResult(status=TaskResultStatus.COMPLETED, summary="ok")

    async def tick(self, ctx: AgentContext) -> int:
        self.calls.append(ctx.clock())
        number = len(self.calls)
        if number in self.fail_on:
            raise RuntimeError(f"tick {number} exploded")
        if number == self.hang_on:
            await asyncio.sleep(3600)
        return number


class SweepingWarden(BaseAgent):
    """WARDEN stand-in exposing the real hook name; records which tasks were waiting."""

    agent_id = AgentId.WARDEN
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self) -> None:
        super().__init__()
        self.sweeps: list[list[UUID]] = []

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        return TaskResult(status=TaskResultStatus.COMPLETED, summary="ok")

    async def resume_waiting_tasks(self, ctx: AgentContext) -> list[UUID]:
        async with ctx.sessions() as session:
            waiting = list(
                await session.scalars(
                    select(TaskRecord.id).where(
                        TaskRecord.state == TaskStatus.WAITING_FOR_APPROVAL.value
                    )
                )
            )
        self.sweeps.append(waiting)
        return waiting


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'hooks.db').as_posix()}")
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield db
    await db.close()


@pytest.fixture
def sessions(database: Database) -> async_sessionmaker[AsyncSession]:
    return database.sessions


@pytest_asyncio.fixture
async def objective_id(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    return await ObjectiveRepository(sessions).create(title="Runtime hooks")


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    logger = logging.getLogger("aether")
    handlers = logger.handlers[:]
    level, propagate = logger.level, logger.propagate
    logger.handlers = []
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def build_engine(
    sessions: async_sessionmaker[AsyncSession],
    agents: list[BaseAgent],
    *,
    clock: Callable[[], datetime] | None = None,
    **limits: Any,
) -> Engine:
    options: dict[str, Any] = {
        "scheduler_interval_seconds": 0.02,
        "message_poll_seconds": 0.02,
        "recovery_interval_seconds": 0.05,
        "lease_ttl_seconds": 30.0,
        "lease_renew_seconds": 0.05,
        "shutdown_timeout_seconds": 0.5,
        "tick_poll_seconds": 0.01,
        "task_control_poll_seconds": 0.02,
        "task_interrupt_grace_seconds": 0.05,
    }
    options.update(limits)
    extra = {"clock": clock} if clock is not None else {}
    return Engine(
        sessions,
        agents,
        Scheduler(SchedulerPolicy(max_concurrent_tasks=4), sessions),
        FakeMonitor(),
        EngineLimits(**options),
        **extra,
    )


async def wait_until(predicate: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def make_task(
    sessions: async_sessionmaker[AsyncSession],
    objective_id: UUID,
    *,
    state: TaskStatus = TaskStatus.READY,
    agent: str = "FORGE",
) -> UUID:
    async with sessions() as session, session.begin():
        task = TaskRecord(
            objective_id=objective_id, title="task", state=state.value, assigned_agent=agent
        )
        session.add(task)
        await session.flush()
        return task.id


async def get_task(sessions: async_sessionmaker[AsyncSession], task_id: UUID) -> TaskRecord:
    async with sessions() as session:
        task = await session.get(TaskRecord, task_id, populate_existing=True)
    assert task is not None
    return task


async def events(
    sessions: async_sessionmaker[AsyncSession], event_type: str, aggregate_id: UUID | None = None
) -> list[EventRecord]:
    async with sessions() as session:
        statement = select(EventRecord).where(EventRecord.event_type == event_type)
        if aggregate_id is not None:
            statement = statement.where(EventRecord.aggregate_id == aggregate_id)
        return list(await session.scalars(statement.order_by(EventRecord.created_at)))


async def transitions(sessions: async_sessionmaker[AsyncSession], task_id: UUID) -> list[str]:
    return [
        f"{row.payload['from_status']}>{row.payload['to_status']}"
        for row in await events(sessions, "task.transitioned", task_id)
    ]


def _reachable(root: object) -> list[object]:
    """Every object reachable through instance attributes, slots and containers of aether code."""
    seen: dict[int, object] = {}
    pending = [root]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen[id(item)] = item
        if isinstance(item, dict):
            pending += list(item.keys()) + list(item.values())
        elif isinstance(item, list | tuple | set | frozenset):
            pending += list(item)
        elif type(item).__module__.startswith("aether") and not isinstance(item, type):
            names = [n for c in type(item).__mro__ for n in getattr(c, "__slots__", ())]
            names += list(getattr(item, "__dict__", {}))
            pending += [getattr(item, name, None) for name in names if not name.startswith("__")]
    return list(seen.values())


# --- services injection -------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> AetherSettings:
    return AetherSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'runtime.db').as_posix()}",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        artifact_dir=tmp_path / "artifacts",
        workspace_dir=tmp_path / "workspaces",
    )


@pytest_asyncio.fixture
async def runtime(settings: AetherSettings) -> AsyncIterator[Runtime]:
    runtime = build_runtime(
        settings,
        security_settings=SecuritySettings(_env_file=None),
        runtime_settings=AgentRuntimeSettings(
            _env_file=None,
            orchestr=OrchestrLimits(max_active_objectives=4),
            research=ResearchLimits(max_sources=2),
        ),
    )
    async with runtime.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield runtime
    await runtime.close()


async def test_each_agent_receives_only_the_services_it_needs(
    runtime: Runtime, settings: AetherSettings
) -> None:
    engine = runtime.engine
    services = {agent_id: engine.context_for(agent_id).services for agent_id in ALL_AGENTS}

    orchestr = services[AgentId.ORCHESTR]
    assert set(orchestr) == ORCHESTR_KEYS | {"task_control"}
    assert isinstance(orchestr["resource_monitor"], ResourceMonitor)
    assert isinstance(orchestr["scheduler_policy"], SchedulerPolicy)
    assert orchestr["disk_path"] == settings.data_dir
    assert orchestr["orchestr_limits"] == OrchestrLimits(max_active_objectives=4)
    assert isinstance(orchestr["task_control"], TaskControl)

    warden = services[AgentId.WARDEN]
    assert set(warden) == PRIVILEGED_KEYS
    assert warden["approvals"] is runtime.security.approvals
    assert warden["security_policy"] is runtime.security.policy
    assert runtime.security.approvals.policy is runtime.security.policy

    assert set(services[AgentId.MUSE]) == {"muse_settings"}
    assert services[AgentId.MUSE]["muse_settings"] == MuseSettings(data_dir=settings.data_dir)
    for agent_id in (AgentId.FORGE, AgentId.LEDGER, AgentId.SCOUT, AgentId.ROGUE):
        assert services[agent_id] == {}

    for agent_id in ALL_AGENTS:
        reachable = _reachable(engine.context_for(agent_id))
        approvals = [item for item in reachable if isinstance(item, ApprovalService)]
        policies = [item for item in reachable if isinstance(item, SecurityPolicy)]
        assert bool(approvals) is (agent_id is AgentId.WARDEN), agent_id
        assert bool(policies) is (agent_id is AgentId.WARDEN), agent_id


async def test_agents_are_constructed_with_runtime_settings(
    runtime: Runtime, settings: AetherSettings
) -> None:
    engine = runtime.engine
    muse = engine.agent(AgentId.MUSE)
    assert isinstance(muse, MuseAgent)
    assert muse.settings.data_dir == settings.data_dir
    for agent_id in (AgentId.SCOUT, AgentId.ROGUE):
        assert engine.agent(agent_id).limits == ResearchLimits(max_sources=2)  # type: ignore[attr-defined]
    assert engine.tick_hooks == {
        AgentId.ORCHESTR: "tick",
        AgentId.WARDEN: "resume_waiting_tasks",
        AgentId.MUSE: "resume_pending",
    }


async def test_interpreter_is_allowlisted_for_verification_runs(runtime: Runtime) -> None:
    policy = runtime.security.policy
    assert policy.resolve_executable(sys.executable) is not None
    # Nothing else is relaxed: subprocesses stay limited to FORGE/LEDGER and need approval.
    runners = {a for a in ALL_AGENTS if policy.permits(a, OperationKind.SUBPROCESS)}
    assert runners == {AgentId.FORGE, AgentId.LEDGER}
    assert policy.requires_approval(OperationKind.SUBPROCESS)


async def test_non_warden_agents_cannot_be_given_warden_services(sessions) -> None:
    with pytest.raises(ValueError, match="WARDEN-only"):
        Engine(
            sessions,
            [WorkAgent()],
            Scheduler(SchedulerPolicy(), sessions),
            FakeMonitor(),
            agent_services={AgentId.FORGE: {"approvals": object()}},
        )


async def test_run_once_still_exits_cleanly_with_hooks(
    runtime: Runtime, isolated_logging: None
) -> None:
    await runtime.run(once=True)

    assert runtime.engine.state is LifecycleState.STOPPED
    sessions = runtime.database.sessions
    assert await events(sessions, "error.raised") == []
    assert [event.event_type for event in await events(sessions, "runtime.stopped")]


# --- periodic ticks -----------------------------------------------------------------------


async def test_ticks_on_start_and_every_interval_with_a_fake_clock(sessions) -> None:
    clock = FakeClock()
    ticker = TickingAgent()
    warden = SweepingWarden()
    engine = build_engine(
        sessions,
        [ticker, warden, WorkAgent()],
        clock=clock,
        agent_tick_seconds=10.0,
        agent_tick_overrides={AgentId.WARDEN: 100.0},
    )
    await engine.start()
    try:
        await wait_until(lambda: _true(len(ticker.calls) == 1 and len(warden.sweeps) == 1))
        clock.advance(5)
        await asyncio.sleep(0.1)
        assert len(ticker.calls) == 1
        clock.advance(6)
        await wait_until(lambda: _true(len(ticker.calls) == 2))
        clock.advance(11)
        await wait_until(lambda: _true(len(ticker.calls) == 3))
        assert len(warden.sweeps) == 1  # its own, longer interval
        assert ticker.calls == [
            clock.now - timedelta(seconds=22),
            clock.now - timedelta(seconds=11),
            clock.now,
        ]
    finally:
        await engine.stop()
    assert engine.tick_hooks == {AgentId.ORCHESTR: "tick", AgentId.WARDEN: "resume_waiting_tasks"}


async def test_tick_failures_are_recorded_and_the_loop_continues(sessions) -> None:
    clock = FakeClock()
    ticker = TickingAgent(fail_on=frozenset({1}), hang_on=2)
    engine = build_engine(
        sessions,
        [ticker],
        clock=clock,
        agent_tick_seconds=10.0,
        agent_tick_timeout_seconds=0.05,
    )
    await engine.start()

    async def recorded(count: int) -> bool:
        return len(await events(sessions, "error.raised", engine.run_id)) == count

    try:
        await wait_until(lambda: recorded(1))
        clock.advance(10)
        await wait_until(lambda: recorded(2))
        clock.advance(10)
        await wait_until(lambda: _true(len(ticker.calls) == 3))
    finally:
        await engine.stop()

    errors = await events(sessions, "error.raised", engine.run_id)
    assert [(e.payload["error_type"], e.payload["details"]) for e in errors] == [
        ("RuntimeError", {"agent_id": "ORCHESTR", "hook": "tick"}),
        ("TimeoutError", {"agent_id": "ORCHESTR", "hook": "tick"}),
    ]
    assert errors[0].payload["message"] == "tick 1 exploded"
    assert await engine.run_agent_tick(AgentId.ORCHESTR) == 4
    assert await engine.run_agent_tick(AgentId.FORGE) is None  # no hook: nothing to do


async def test_waiting_for_approval_wakes_the_warden_sweep(sessions, objective_id) -> None:
    async def wait_for_approval(_agent: BaseAgent, _task: TaskRecord) -> TaskResult:
        return TaskResult(status=TaskResultStatus.WAITING_FOR_APPROVAL, summary="needs review")

    warden = SweepingWarden()
    engine = build_engine(
        sessions, [warden, WorkAgent(wait_for_approval)], agent_tick_seconds=3600.0
    )
    await engine.start()
    try:
        await wait_until(lambda: _true(len(warden.sweeps) == 1))
        task_id = await make_task(sessions, objective_id)  # picked up by the scheduler
        await wait_until(lambda: _true(len(warden.sweeps) == 2))
    finally:
        await engine.stop()
    assert warden.sweeps == [[], [task_id]]


# --- cooperative pause / cancel -----------------------------------------------------------


async def _cooperative(agent: BaseAgent, task: TaskRecord) -> TaskResult:
    while not agent.is_cancelled(task.id):
        await asyncio.sleep(0.01)
    return TaskResult(status=TaskResultStatus.BLOCKED, summary="stopped on request")


async def _stubborn(_agent: BaseAgent, _task: TaskRecord) -> TaskResult:
    await asyncio.sleep(3600)
    raise AssertionError("unreachable")


async def test_pause_of_a_running_task_is_cooperative_and_refunds_the_attempt(
    sessions, objective_id
) -> None:
    agent = WorkAgent(_cooperative)
    engine = build_engine(sessions, [agent], task_interrupt_grace_seconds=30.0)
    task_id = await make_task(sessions, objective_id)
    assert await engine.pool.submit(task_id, agent)
    await agent.started.wait()

    result = await engine.task_control.request_pause(task_id, reason="operator asked")
    assert (result.outcome, result.state) == ("pending", TaskStatus.RUNNING)
    await engine.pool.join()

    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == ("paused", 0, None)
    assert await transitions(sessions, task_id) == [
        "ready>scheduled",
        "scheduled>running",
        "running>paused",
    ]
    assert await engine.task_control.pending(task_id) == []
    resolved = [
        e
        for e in await events(sessions, "decision.recorded", task_id)
        if e.payload["decision"] == CONTROL_RESOLVED
    ]
    assert [e.payload["rationale"] for e in resolved] == ["paused"]
    assert not agent.is_cancelled(task_id)  # cleared so the task can run again

    assert await engine.task_control.resume(task_id)
    assert (await get_task(sessions, task_id)).state == TaskStatus.READY.value
    assert await engine.pool.submit(task_id, WorkAgent())
    await engine.pool.join()
    assert (await get_task(sessions, task_id)).state == TaskStatus.COMPLETED.value


async def test_cancel_interrupts_an_unresponsive_task_after_the_grace(
    sessions, objective_id
) -> None:
    agent = WorkAgent(_stubborn)
    engine = build_engine(sessions, [agent], task_interrupt_grace_seconds=0.05)
    task_id = await make_task(sessions, objective_id)
    assert await engine.pool.submit(task_id, agent)
    await agent.started.wait()

    await engine.task_control.request_cancel(task_id)
    await asyncio.wait_for(engine.pool.join(), 5)

    assert (await get_task(sessions, task_id)).state == TaskStatus.CANCELLED.value
    assert (await transitions(sessions, task_id))[-2:] == ["running>blocked", "blocked>cancelled"]
    cancelled = await events(sessions, "task.cancelled", task_id)
    assert cancelled[0].payload["reason"] == "cancel_requested"
    assert await engine.task_control.pending(task_id) == []


async def test_completion_wins_over_a_concurrent_cancel(sessions, objective_id) -> None:
    finish = asyncio.Event()

    async def finish_anyway(_agent: BaseAgent, _task: TaskRecord) -> TaskResult:
        await finish.wait()
        return TaskResult(status=TaskResultStatus.COMPLETED, summary="done regardless")

    agent = WorkAgent(finish_anyway)
    engine = build_engine(sessions, [agent], task_interrupt_grace_seconds=30.0)
    task_id = await make_task(sessions, objective_id)
    assert await engine.pool.submit(task_id, agent)
    await agent.started.wait()

    await engine.task_control.request_cancel(task_id)
    await wait_until(lambda: _true(agent.is_cancelled(task_id)))
    finish.set()
    await engine.pool.join()

    assert (await get_task(sessions, task_id)).state == TaskStatus.COMPLETED.value
    assert "running>blocked" not in await transitions(sessions, task_id)
    resolved = [
        e
        for e in await events(sessions, "decision.recorded", task_id)
        if e.payload["decision"] == CONTROL_RESOLVED
    ]
    assert [e.payload["rationale"] for e in resolved] == ["superseded: task completed"]
    assert await engine.task_control.pending(task_id) == []
    # A late request against the finished task is a no-op.
    late = await engine.task_control.request_pause(task_id)
    assert (late.outcome, late.state) == ("noop", TaskStatus.COMPLETED)


async def test_requests_for_idle_tasks_apply_directly_with_compare_and_set(
    sessions, objective_id
) -> None:
    control = build_engine(sessions, [WorkAgent()]).task_control
    task_id = await make_task(sessions, objective_id)

    paused = await control.request_pause(task_id)
    assert (paused.outcome, paused.state) == ("applied", TaskStatus.PAUSED)
    again = await control.request_pause(task_id)
    assert (again.outcome, again.state) == ("noop", TaskStatus.PAUSED)
    cancelled = await control.request_cancel(task_id)
    assert (cancelled.outcome, cancelled.state) == ("applied", TaskStatus.CANCELLED)
    assert await transitions(sessions, task_id) == ["ready>paused", "paused>cancelled"]
    assert await control.pending(task_id) == []
    with pytest.raises(LookupError):
        await control.request_cancel(UUID(int=1))


async def test_deferred_pause_is_applied_before_the_agent_runs(sessions, objective_id) -> None:
    agent = WorkAgent()
    engine = build_engine(sessions, [agent])
    task_id = await make_task(sessions, objective_id, state=TaskStatus.WAITING_FOR_APPROVAL)

    deferred = await engine.task_control.request_pause(task_id)
    assert (deferred.outcome, deferred.state) == ("pending", TaskStatus.WAITING_FOR_APPROVAL)
    async with sessions() as session, session.begin():
        task = await session.get(TaskRecord, task_id)
        assert task is not None
        task.state = TaskStatus.READY.value  # e.g. WARDEN approved it

    assert await engine.pool.submit(task_id, agent)
    await engine.pool.join()

    assert agent.executed == []
    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt) == (TaskStatus.PAUSED.value, 0)
    assert await engine.task_control.pending(task_id) == []


async def test_orchestr_task_control_records_requests_as_orchestr(sessions, objective_id) -> None:
    engine = build_engine(sessions, [TickingAgent(), WorkAgent()])
    control = engine.context_for(AgentId.ORCHESTR).services["task_control"]
    assert isinstance(control, TaskControl)
    assert "task_control" not in engine.context_for(AgentId.FORGE).services
    task_id = await make_task(sessions, objective_id)

    await control.request_cancel(task_id, reason="objective abandoned")

    requests = [
        e
        for e in await events(sessions, "decision.recorded", task_id)
        if e.payload["decision"] == "task.cancel_requested"
    ]
    assert [(e.actor, e.payload["rationale"]) for e in requests] == [
        ("ORCHESTR", "objective abandoned")
    ]


async def _true(value: bool) -> bool:
    return value
