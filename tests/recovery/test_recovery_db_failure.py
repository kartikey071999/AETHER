"""Scenario 3: the database commit fails after the statements of a transaction were sent.

Each storage operation commits its row changes and its audit events in one transaction, so a
lost commit leaves neither behind and the operation can simply be retried. In the engine the
lost commit of a task outcome leaves the task RUNNING under its lease; the lease expires and
the task is re-run, ending with exactly one recorded outcome.
"""

from datetime import timedelta

import pytest
from reliability_support import (
    FailingCommits,
    RecordingAgent,
    assign_message,
    build_engine,
    engine_limits,
    event_counts,
    get_task,
    make_task,
    message_status,
    task_state,
    wait_until,
)
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from aether.communication.bus import MessageBus
from aether.runtime.leases import LeaseManager
from aether.storage.models import EventRecord, MessageRecord, TaskStatus
from aether.storage.repositories import TaskRepository

TTL = timedelta(seconds=30)


@pytest.fixture
def flaky(database) -> FailingCommits:
    return FailingCommits(database.engine)


async def test_failed_commit_of_a_transition_leaves_no_partial_state(
    sessions, flaky, objective_id
) -> None:
    task_id = await make_task(sessions, objective_id, state=TaskStatus.PENDING)
    before = await event_counts(sessions, task_id)
    tasks = TaskRepository(flaky.sessions)

    flaky.fail_next(1)
    with pytest.raises(OperationalError):
        await tasks.transition(task_id, TaskStatus.READY, actor="ORCHESTR")

    assert flaky.failures == 1
    assert await task_state(sessions, task_id) is TaskStatus.PENDING
    assert await event_counts(sessions, task_id) == before

    await tasks.transition(task_id, TaskStatus.READY, actor="ORCHESTR")  # the retry succeeds
    assert await task_state(sessions, task_id) is TaskStatus.READY
    assert (await event_counts(sessions, task_id))["task.transitioned"] == 1


async def test_failed_lease_acquire_does_not_consume_an_attempt(
    sessions, flaky, objective_id
) -> None:
    task_id = await make_task(sessions, objective_id, max_attempts=1)
    leases = LeaseManager(flaky.sessions)

    flaky.fail_next(1)
    with pytest.raises(OperationalError):
        await leases.acquire(task_id, "engine-a/worker", TTL)

    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == (TaskStatus.READY, 0, None)
    assert "task.lease_acquired" not in await event_counts(sessions, task_id)

    lease = await leases.acquire(task_id, "engine-a/worker", TTL)
    assert lease is not None and lease.attempt == 1


async def test_failed_release_keeps_the_lease_until_a_retry_succeeds(
    sessions, flaky, objective_id
) -> None:
    task_id = await make_task(sessions, objective_id)
    leases = LeaseManager(flaky.sessions)
    assert await leases.acquire(task_id, "engine-a/worker", TTL) is not None

    flaky.fail_next(1)
    with pytest.raises(OperationalError):
        await leases.release(
            task_id, "engine-a/worker", TaskStatus.COMPLETED, outcome_event="task.completed"
        )

    task = await get_task(sessions, task_id)
    assert (task.state, task.lease_owner) == (TaskStatus.RUNNING, "engine-a/worker")
    assert "task.completed" not in await event_counts(sessions, task_id)

    assert await leases.release(
        task_id, "engine-a/worker", TaskStatus.COMPLETED, outcome_event="task.completed"
    )
    assert await task_state(sessions, task_id) is TaskStatus.COMPLETED
    assert (await event_counts(sessions, task_id))["task.completed"] == 1


async def test_failed_publish_can_be_retried_without_duplicating_the_message(
    sessions, flaky
) -> None:
    bus = MessageBus(flaky.sessions)
    message = assign_message()

    flaky.fail_next(1)
    with pytest.raises(OperationalError):
        await bus.publish(message)

    async with sessions() as session:
        assert await session.get(MessageRecord, message.message_id) is None
        assert await session.scalar(select(func.count()).select_from(EventRecord)) == 0

    assert await bus.publish(message)
    assert not await bus.publish(message)
    assert await message_status(sessions, message.message_id) == "pending"
    assert (await event_counts(sessions, message.message_id))["message.published"] == 1


async def test_engine_reruns_a_task_whose_outcome_commit_was_lost(
    database, flaky, objective_id
) -> None:
    sessions = flaky.sessions
    agent = RecordingAgent()
    engine = build_engine(
        sessions,
        [agent],
        limits=engine_limits(lease_ttl_seconds=0.3, lease_renew_seconds=0.1),
    )
    task_id = await make_task(sessions, objective_id, max_attempts=3)
    flaky.fail_next(1, on_event="task.completed")

    await engine.start()
    try:
        await wait_until(
            lambda: _is(database.sessions, task_id, TaskStatus.COMPLETED), timeout=10.0
        )
    finally:
        await engine.stop()

    assert flaky.failures == 1
    assert agent.executed == [task_id, task_id]  # the lost outcome forced one re-run
    task = await get_task(database.sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == (TaskStatus.COMPLETED, 2, None)
    counts = await event_counts(database.sessions, task_id)
    assert counts["task.completed"] == 1
    assert counts["task.lease_expired"] == 1


async def _is(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state
