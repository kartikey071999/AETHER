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
    RecordingAgent,
    assign_message,
    build_engine,
    event_counts,
    get_task,
    make_task,
    message_status,
    postgres_databases,
    task_state,
    wait_until,
)

from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.runtime.leases import LeaseManager
from aether.storage.database import Database
from aether.storage.models import TaskStatus
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
    # Every engine is up before any message is published, so start-up requeueing cannot touch
    # a peer's in-flight messages (see the multi-engine note in the T-050 report).
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
