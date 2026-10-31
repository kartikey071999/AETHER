import asyncio
import json
from datetime import UTC, datetime, timedelta

from runtime_helpers import FakeMonitor, StubAgent, event_counts, get_task, make_task, wait_until
from sqlalchemy import select

from aether.agents.base import TaskResult, TaskResultStatus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.lifecycle import LifecycleState
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.storage.models import EventRecord, MessageRecord, TaskRecord, TaskStatus


def build_engine(sessions, agents, **limits) -> Engine:
    options = {
        "max_concurrent_tasks": 2,
        "scheduler_interval_seconds": 0.02,
        "message_poll_seconds": 0.02,
        "recovery_interval_seconds": 0.05,
        "lease_ttl_seconds": 30.0,
        "lease_renew_seconds": 0.05,
        "shutdown_timeout_seconds": 0.1,
    }
    options.update(limits)
    return Engine(
        sessions,
        agents,
        Scheduler(SchedulerPolicy(max_concurrent_tasks=4), sessions),
        FakeMonitor(),
        EngineLimits(**options),
    )


def assign_message(**overrides) -> AgentMessage:
    values = {
        "source_agent": AgentId.ORCHESTR,
        "target_agent": AgentId.FORGE,
        "message_type": MessageType.TASK_ASSIGN,
        "payload": {},
    }
    values.update(overrides)
    return AgentMessage(**values)


async def task_in_state(sessions, task_id, state: TaskStatus) -> bool:
    return (await get_task(sessions, task_id)).state == state


async def all_in_state(sessions, task_ids, state: TaskStatus) -> bool:
    return all([await task_in_state(sessions, task_id, state) for task_id in task_ids])


async def messages_settled(sessions, statuses: tuple[str, ...]) -> bool:
    async with sessions() as session:
        rows = await session.scalars(select(MessageRecord))
        return all(row.status in statuses for row in rows)


async def runtime_events(sessions, engine: Engine) -> list[str]:
    async with sessions() as session:
        rows = await session.scalars(
            select(EventRecord)
            .where(EventRecord.aggregate_id == engine.run_id)
            .order_by(EventRecord.created_at)
        )
        return [event.event_type for event in rows]


async def test_engine_runs_ready_tasks_to_completion(sessions, objective_id) -> None:
    agent = StubAgent()
    engine = build_engine(sessions, [agent])
    task_id = await make_task(sessions, objective_id)

    await engine.start()
    try:
        await wait_until(lambda: task_in_state(sessions, task_id, TaskStatus.COMPLETED))
    finally:
        await engine.stop()

    assert agent.started_tasks == [task_id]
    counts = await event_counts(sessions)
    assert counts["scheduler.decision"] >= 1 and counts["task.completed"] == 1


async def test_engine_ignores_tasks_without_a_registered_agent(sessions, objective_id) -> None:
    engine = build_engine(sessions, [StubAgent()])
    unassigned = await make_task(sessions, objective_id, agent=None)
    foreign = await make_task(sessions, objective_id, agent="SCOUT")

    assert await engine.schedule_once() == 0  # not running yet
    await engine.start()
    await asyncio.sleep(0.1)
    await engine.stop()

    assert (await get_task(sessions, unassigned)).state == TaskStatus.READY
    assert (await get_task(sessions, foreign)).state == TaskStatus.READY


async def test_engine_respects_concurrency_limit(sessions, objective_id) -> None:
    release = asyncio.Event()

    async def hold(_task: TaskRecord) -> TaskResult:
        await release.wait()
        return TaskResult(status=TaskResultStatus.COMPLETED)

    agent = StubAgent(hold)
    engine = build_engine(sessions, [agent], max_concurrent_tasks=2)
    ids = [await make_task(sessions, objective_id) for _ in range(4)]

    await engine.start()
    try:
        await wait_until(lambda: asyncio.sleep(0, len(agent.started_tasks) == 2))
        await asyncio.sleep(0.1)
        assert len(agent.started_tasks) == 2 and agent.max_active == 2
        release.set()
        await wait_until(lambda: all_in_state(sessions, ids, TaskStatus.COMPLETED))
    finally:
        release.set()
        await engine.stop()

    assert agent.max_active == 2


async def test_stop_emits_events_and_leaves_no_leased_tasks(sessions, objective_id) -> None:
    started = asyncio.Event()

    async def block(_task: TaskRecord) -> TaskResult:
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    agent = StubAgent(block)
    engine = build_engine(sessions, [agent])
    task_id = await make_task(sessions, objective_id)

    await engine.start()
    await asyncio.wait_for(started.wait(), 3.0)
    assert (await get_task(sessions, task_id)).state == TaskStatus.RUNNING

    await engine.stop(timeout=0.05)
    await engine.stop()  # idempotent

    assert engine.state is LifecycleState.STOPPED
    events = await runtime_events(sessions, engine)
    assert events == ["runtime.started", "runtime.stopping", "runtime.stopped"]
    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.READY and task.lease_owner is None and task.attempt == 0
    async with sessions() as session:
        statement = select(TaskRecord).where(TaskRecord.lease_owner.is_not(None))
        leased = (await session.scalars(statement)).all()
    assert leased == []
    assert (await agent.heartbeat()).status == "unhealthy"  # agent was stopped


async def test_stopping_an_unstarted_engine_is_safe(sessions) -> None:
    engine = build_engine(sessions, [StubAgent()])

    await engine.stop()

    assert engine.state is LifecycleState.STOPPED
    assert await runtime_events(sessions, engine) == []


async def test_stop_does_not_wait_for_idle_message_loops_to_poll(sessions) -> None:
    # Regression: an idle agent loop only noticed shutdown when its message poll timed out, so
    # every stop took up to message_poll_seconds (1s by default; here 30s).
    engine = build_engine(
        sessions, [StubAgent()], message_poll_seconds=30.0, shutdown_timeout_seconds=30.0
    )
    await engine.start()
    await asyncio.sleep(0.1)  # the message loop is now idle, waiting for messages

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(engine.stop(), 15.0)

    assert asyncio.get_running_loop().time() - started < 10.0
    assert engine.state is LifecycleState.STOPPED


async def test_start_recovers_stale_leases_and_messages(sessions, objective_id) -> None:
    async with sessions() as session, session.begin():
        stale = TaskRecord(
            objective_id=objective_id,
            title="stale",
            state=TaskStatus.RUNNING.value,
            assigned_agent=None,
            attempt=1,
            max_attempts=3,
            lease_owner="dead-engine/x",
            lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session.add(stale)
        await session.flush()
        stale_id = stale.id

    agent = StubAgent()
    engine = build_engine(sessions, [agent])
    in_flight = assign_message()
    pending = assign_message()
    await engine.bus.publish(in_flight)
    await engine.bus.publish(pending)
    await engine.bus.claim(AgentId.FORGE, 1, consumer="dead-engine")  # left delivered

    await engine.start()
    try:
        assert engine.recovery_report is not None
        assert [o.task_id for o in engine.recovery_report.recovered_tasks] == [stale_id]
        assert engine.recovery_report.requeued_messages == 1
        await wait_until(lambda: messages_settled(sessions, ("processed",)))
    finally:
        await engine.stop()

    assert (await get_task(sessions, stale_id)).state == TaskStatus.READY
    assert {m.message_id for m in agent.handled} == {in_flight.message_id, pending.message_id}


async def test_unaccepted_message_types_fail_with_structured_error(sessions) -> None:
    agent = StubAgent()
    engine = build_engine(sessions, [agent])
    bad = assign_message(message_type=MessageType.RESEARCH_FINDING)
    good = assign_message()
    await engine.bus.publish(bad)
    await engine.bus.publish(good)

    await engine.start()
    try:
        await wait_until(lambda: messages_settled(sessions, ("processed", "failed")))
    finally:
        await engine.stop()

    async with sessions() as session:
        failure = await session.scalar(
            select(EventRecord).where(EventRecord.event_type == "message.failed")
        )
    assert failure is not None
    assert json.loads(failure.payload["error"])["error"] == "unsupported_message_type"
    assert [m.message_id for m in agent.handled] == [good.message_id]


async def test_handler_errors_mark_message_failed_without_stopping_the_loop(sessions) -> None:
    class Flaky(StubAgent):
        async def handle_message(self, message, ctx) -> None:
            if message.payload.get("explode"):
                raise RuntimeError("handler broke")
            await super().handle_message(message, ctx)

    agent = Flaky()
    engine = build_engine(sessions, [agent])
    broken = assign_message(payload={"explode": True})
    fine = assign_message()
    await engine.bus.publish(broken)
    await engine.bus.publish(fine)

    await engine.start()
    try:
        await wait_until(lambda: messages_settled(sessions, ("processed", "failed")))
    finally:
        await engine.stop()

    async with sessions() as session:
        statuses = {row.id: row.status for row in await session.scalars(select(MessageRecord))}
    assert statuses == {broken.message_id: "failed", fine.message_id: "processed"}


async def test_run_forever_stops_when_requested(sessions, objective_id) -> None:
    engine = build_engine(sessions, [StubAgent()])
    runner = asyncio.create_task(engine.run_forever())

    async def running() -> bool:
        return engine.state is LifecycleState.RUNNING

    await wait_until(running)
    engine.request_stop()
    await asyncio.wait_for(runner, 3.0)

    assert engine.state is LifecycleState.STOPPED
    assert (await runtime_events(sessions, engine))[-1] == "runtime.stopped"


async def test_engine_health_reports_each_agent(sessions) -> None:
    engine = build_engine(sessions, [StubAgent()])
    await engine.start()
    try:
        report = await engine.health()
    finally:
        await engine.stop()

    assert report[AgentId.FORGE].status == "healthy"


async def test_engine_injects_a_work_queue_bound_to_each_agent(sessions, objective_id) -> None:
    agent = StubAgent()
    engine = build_engine(sessions, [agent])
    ctx = engine.context_for(AgentId.FORGE)

    assert ctx.work is not None
    assert ctx.work.agent_id is AgentId.FORGE
    assert agent.work_queue(ctx) is ctx.work
    task_id = await make_task(sessions, objective_id)
    assert [item.id for item in await ctx.work.open_tasks()] == [task_id]
