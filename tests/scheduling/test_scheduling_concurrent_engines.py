"""Scenario 8: several runtimes (separate connection pools) share one database.

Leases are compare-and-set updates and message claims are compare-and-set (plus SKIP LOCKED on
PostgreSQL), so no task runs twice and no message is delivered to two consumers, however the
engines interleave.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from reliability_support import (
    FakeClock,
    RecordingAgent,
    assign_message,
    build_engine,
    engine_limits,
    event_counts,
    events_of,
    get_task,
    make_task,
    message_status,
    postgres_databases,
    task_state,
    wait_until,
)

from aether.agents.base import AgentContext
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage
from aether.runtime.leases import LeaseManager
from aether.storage.database import Database
from aether.storage.models import MessageRecord, TaskStatus
from aether.storage.repositories import ObjectiveRepository

TTL = timedelta(seconds=30)
TASKS = 12


async def _lease_race(databases: list[Database], objective_id: UUID) -> None:
    sessions = databases[0].sessions
    task_ids = [await make_task(sessions, objective_id, title=f"t{n}") for n in range(TASKS)]
    managers = [LeaseManager(database.sessions) for database in databases]

    for task_id in task_ids:
        leases = await asyncio.gather(
            *(
                manager.acquire(task_id, f"engine-{index}/worker", TTL)
                for index, manager in enumerate(managers)
            )
        )
        winners = [lease for lease in leases if lease is not None]
        assert len(winners) == 1, leases
        task = await get_task(sessions, task_id)
        assert (task.state, task.attempt, task.lease_owner) == (
            TaskStatus.RUNNING,
            1,
            winners[0].owner,
        )
        assert (await event_counts(sessions, task_id))["task.lease_acquired"] == 1


async def _engines_race(databases: list[Database], objective_id: UUID) -> None:
    sessions = databases[0].sessions
    task_ids = [await make_task(sessions, objective_id, title=f"t{n}") for n in range(TASKS)]
    agents = [RecordingAgent() for _ in databases]
    engines = [
        build_engine(database.sessions, [agent])
        for database, agent in zip(databases, agents, strict=True)
    ]
    # Every engine is up before any message is published; a late engine joining while messages
    # are in flight is covered by the claim tests below.
    for engine in engines:
        await engine.start()
    try:
        messages = [assign_message() for _ in range(TASKS)]
        for message in messages:
            await MessageBus(sessions).publish(message)

        async def settled() -> bool:
            states = [await task_state(sessions, task_id) for task_id in task_ids]
            statuses = [await message_status(sessions, m.message_id) for m in messages]
            return all(state is TaskStatus.COMPLETED for state in states) and all(
                status == "processed" for status in statuses
            )

        await wait_until(settled, timeout=20.0)
    finally:
        for engine in engines:
            await engine.stop()

    executed = [task_id for agent in agents for task_id in agent.executed]
    assert sorted(executed) == sorted(task_ids)  # each task ran exactly once overall
    handled = [message_id for agent in agents for message_id in agent.handled]
    assert sorted(handled) == sorted(message.message_id for message in messages)
    for task_id in task_ids:
        task = await get_task(sessions, task_id)
        assert (task.attempt, task.lease_owner) == (1, None)
        counts = await event_counts(sessions, task_id)
        assert counts["task.lease_acquired"] == counts["task.completed"] == 1


async def _claim_race(databases: list[Database]) -> None:
    messages = [assign_message() for _ in range(TASKS)]
    for message in messages:
        await MessageBus(databases[0].sessions).publish(message)

    batches = await asyncio.gather(
        *(
            MessageBus(database.sessions).claim(AgentId.FORGE, TASKS, consumer=f"engine-{n}")
            for n, database in enumerate(databases)
            for _ in range(2)
        )
    )

    claimed = [message.message_id for batch in batches for message in batch]
    assert sorted(claimed) == sorted(message.message_id for message in messages)


async def test_two_runtimes_racing_for_the_same_tasks_lease_each_once(
    database, peer_database, objective_id
) -> None:
    await _lease_race([database, peer_database], objective_id)


@pytest.mark.slow
async def test_two_running_engines_execute_each_task_and_message_once(
    database, peer_database, objective_id
) -> None:
    await _engines_race([database, peer_database], objective_id)


async def test_two_runtimes_never_claim_the_same_message(database, peer_database) -> None:
    await _claim_race([database, peer_database])


async def test_a_second_engine_recovers_tasks_of_a_dead_peer_but_not_of_a_live_one(
    database, peer_database, objective_id
) -> None:
    sessions = database.sessions
    now = datetime.now(UTC)
    dead = await make_task(
        sessions,
        objective_id,
        state=TaskStatus.RUNNING,
        attempt=1,
        lease_owner="engine-dead/worker",
        lease_expires_at=now - timedelta(seconds=1),
    )
    live = await make_task(
        sessions,
        objective_id,
        state=TaskStatus.RUNNING,
        attempt=1,
        lease_owner="engine-live/worker",
        lease_expires_at=now + timedelta(minutes=10),
    )
    agent = RecordingAgent()
    engine = build_engine(peer_database.sessions, [agent])

    await engine.start()
    try:
        await wait_until(lambda: _is(sessions, dead, TaskStatus.COMPLETED))
        await asyncio.sleep(0.2)
    finally:
        await engine.stop()

    assert agent.executed == [dead]
    task = await get_task(sessions, live)
    assert (task.state, task.lease_owner) == (TaskStatus.RUNNING, "engine-live/worker")


@pytest.mark.postgres
async def test_postgres_lease_race_is_exactly_once() -> None:
    async with postgres_databases(3) as databases:
        objective_id = await ObjectiveRepository(databases[0].sessions).create(title="race")
        await _lease_race(databases, objective_id)


@pytest.mark.postgres
async def test_postgres_engines_execute_each_task_and_message_once() -> None:
    async with postgres_databases(2) as databases:
        objective_id = await ObjectiveRepository(databases[0].sessions).create(title="race")
        await _engines_race(databases, objective_id)


@pytest.mark.postgres
async def test_postgres_claims_skip_locked_rows_without_double_delivery() -> None:
    async with postgres_databases(3) as databases:
        await _claim_race(databases)


async def _is(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state


class BlockingAgent(RecordingAgent):
    """Holds every message it receives in flight until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        self.handled.append(message.message_id)
        await self.release.wait()


async def _status_is(sessions, message_id: UUID, status: str) -> bool:
    return await message_status(sessions, message_id) == status


async def _requeue_events(sessions, message_id: UUID) -> list:
    return await events_of(sessions, "message.requeued", message_id)


async def _late_engine_leaves_live_claims_alone(databases: list[Database]) -> None:
    first_db, second_db = databases[0], databases[1]
    busy, idle = BlockingAgent(), RecordingAgent()
    first = build_engine(first_db.sessions, [busy])
    second = build_engine(second_db.sessions, [idle])
    message = assign_message()
    await first.start()
    try:
        await MessageBus(first_db.sessions).publish(message)
        await wait_until(lambda: _status_is(first_db.sessions, message.message_id, "processing"))

        await second.start()  # starts while the first engine has the message in flight
        try:
            assert second.recovery_report is not None
            assert second.recovery_report.requeued_messages == 0
            await asyncio.sleep(0.4)  # several periodic sweeps of the second engine
            assert await message_status(first_db.sessions, message.message_id) == "processing"
            assert idle.handled == []
        finally:
            busy.release.set()
            await wait_until(lambda: _status_is(first_db.sessions, message.message_id, "processed"))
            await second.stop()
    finally:
        busy.release.set()
        await first.stop()

    assert busy.handled == [message.message_id] and idle.handled == []
    assert await _requeue_events(first_db.sessions, message.message_id) == []


async def _dead_engines_claim_is_taken_over_after_it_expires(databases: list[Database]) -> None:
    first_db, second_db = databases[0], databases[1]
    clock = FakeClock()
    message = assign_message()
    dead_bus = MessageBus(first_db.sessions, clock=clock)
    await dead_bus.publish(message)
    await dead_bus.claim(AgentId.FORGE, 1, consumer="engine-dead")
    await dead_bus.mark_processing(message.message_id, consumer="engine-dead")
    agent = RecordingAgent()
    engine = build_engine(
        second_db.sessions,
        [agent],
        clock=clock,
        bus=MessageBus(second_db.sessions, clock=clock),
    )

    await engine.start()
    try:
        assert engine.recovery_report is not None
        assert engine.recovery_report.requeued_messages == 0  # the dead engine's claim is live
        await asyncio.sleep(0.2)
        assert await message_status(first_db.sessions, message.message_id) == "processing"
        assert agent.handled == []

        clock.advance(120)  # the claim lapses: nobody renewed it

        await wait_until(lambda: _status_is(first_db.sessions, message.message_id, "processed"))
    finally:
        await engine.stop()

    assert agent.handled == [message.message_id]
    [requeued] = await _requeue_events(first_db.sessions, message.message_id)
    assert requeued.payload["consumer"] == "engine-dead"
    assert requeued.payload["reason"] == "claim_expired"


async def test_a_late_engine_does_not_requeue_a_live_engines_messages(
    database, peer_database
) -> None:
    await _late_engine_leaves_live_claims_alone([database, peer_database])


async def test_a_dead_engines_message_is_taken_over_once_its_claim_expires(
    database, peer_database
) -> None:
    await _dead_engines_claim_is_taken_over_after_it_expires([database, peer_database])


async def test_the_engine_heartbeat_renews_its_claims(database) -> None:
    agent = BlockingAgent()
    engine = build_engine(
        database.sessions, [agent], limits=engine_limits(message_claim_ttl_seconds=0.6)
    )
    message = assign_message()

    async def expiry() -> object:
        async with database.sessions() as session:
            record = await session.get(MessageRecord, message.message_id, populate_existing=True)
        assert record is not None
        return record.claim_expires_at

    await engine.start()
    try:
        await MessageBus(database.sessions).publish(message)
        await wait_until(lambda: _status_is(database.sessions, message.message_id, "processing"))
        first = await expiry()

        async def renewed() -> bool:
            return await expiry() != first

        await wait_until(renewed)
    finally:
        agent.release.set()
        await engine.stop()


async def test_a_graceful_stop_releases_claims_for_immediate_pickup(
    database, peer_database
) -> None:
    stuck = BlockingAgent()
    first = build_engine(database.sessions, [stuck])
    message = assign_message()
    await first.start()
    await MessageBus(database.sessions).publish(message)
    await wait_until(lambda: _status_is(database.sessions, message.message_id, "processing"))

    await first.stop()  # the handler never finishes; shutdown gives up on it

    assert await message_status(database.sessions, message.message_id) == "pending"
    [requeued] = await _requeue_events(database.sessions, message.message_id)
    assert requeued.payload["reason"] == "released"
    agent = RecordingAgent()
    second = build_engine(peer_database.sessions, [agent])
    await second.start()
    try:
        await wait_until(lambda: _status_is(database.sessions, message.message_id, "processed"))
    finally:
        await second.stop()
    assert agent.handled == [message.message_id]


@pytest.mark.postgres
async def test_postgres_a_late_engine_does_not_requeue_a_live_engines_messages() -> None:
    async with postgres_databases(2) as databases:
        await _late_engine_leaves_live_claims_alone(databases)


@pytest.mark.postgres
async def test_postgres_a_dead_engines_message_is_taken_over_once_its_claim_expires() -> None:
    async with postgres_databases(2) as databases:
        await _dead_engines_claim_is_taken_over_after_it_expires(databases)


@pytest.mark.postgres
async def test_postgres_concurrent_sweeps_requeue_each_expired_message_once() -> None:
    async with postgres_databases(3) as databases:
        clock = FakeClock()
        messages = [assign_message() for _ in range(TASKS)]
        publisher = MessageBus(databases[0].sessions, clock=clock)
        for message in messages:
            await publisher.publish(message)
        await publisher.claim(AgentId.FORGE, TASKS, consumer="engine-dead")
        clock.advance(120)

        counts = await asyncio.gather(
            *(
                MessageBus(database.sessions, clock=clock).requeue_in_flight()
                for database in databases
                for _ in range(2)
            )
        )

        assert sum(counts) == TASKS
        for message in messages:
            assert len(await _requeue_events(databases[0].sessions, message.message_id)) == 1
