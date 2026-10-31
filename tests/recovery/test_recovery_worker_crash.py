"""Scenario 2: a worker dies while its task is RUNNING and never releases the lease.

The lease expires, recovery returns the task to READY (the crashed attempt counts) so another
worker retries it as attempt + 1, or fails it once ``max_attempts`` is spent. A worker that
comes back after losing its lease cannot overwrite the outcome recorded by its successor.
"""

import asyncio
from datetime import timedelta

from reliability_support import (
    RecordingAgent,
    build_engine,
    engine_limits,
    event_counts,
    events_of,
    get_task,
    make_task,
    task_state,
    wait_until,
)

from aether.agents.base import TaskResult, TaskResultStatus
from aether.runtime.leases import LeaseManager
from aether.storage.models import TaskRecord, TaskStatus

TTL = timedelta(seconds=30)


async def test_expired_lease_is_recovered_and_retried_as_the_next_attempt(
    sessions, objective_id, clock
) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id, max_attempts=3)
    crashed = await leases.acquire(task_id, "engine-aaaa/worker", TTL)
    assert crashed is not None and crashed.attempt == 1
    # The worker process dies here: no release, no renewals.

    clock.advance(TTL.total_seconds() - 1)
    assert await leases.recover_expired() == []  # still within the TTL
    assert await leases.acquire(task_id, "engine-bbbb/worker", TTL) is None

    clock.advance(2)
    [outcome] = await leases.recover_expired()
    assert outcome.new_status is TaskStatus.READY
    assert (outcome.previous_owner, outcome.attempt) == ("engine-aaaa/worker", 1)
    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == (TaskStatus.READY, 1, None)

    retry = await leases.acquire(task_id, "engine-bbbb/worker", TTL)
    assert retry is not None and retry.attempt == 2

    # The crashed worker cannot renew or release a lease it no longer holds.
    assert not await leases.renew(task_id, "engine-aaaa/worker", TTL)
    assert not await leases.release(task_id, "engine-aaaa/worker", TaskStatus.COMPLETED)
    assert await leases.release(task_id, "engine-bbbb/worker", TaskStatus.COMPLETED)
    assert await task_state(sessions, task_id) is TaskStatus.COMPLETED

    [attempted] = await events_of(sessions, "recovery.attempted", task_id)
    assert attempted.payload["success"] is True
    assert attempted.payload["previous_owner"] == "engine-aaaa/worker"
    transitions = [
        (e.payload["from_status"], e.payload["to_status"])
        for e in await events_of(sessions, "task.transitioned", task_id)
    ]
    assert ("running", "retrying") in transitions and ("retrying", "ready") in transitions


async def test_crash_on_the_last_attempt_fails_the_task(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id, max_attempts=2)

    for expected_attempt in (1, 2):
        lease = await leases.acquire(task_id, f"engine-{expected_attempt}/worker", TTL)
        assert lease is not None and lease.attempt == expected_attempt
        clock.advance(TTL.total_seconds() + 1)
        [outcome] = await leases.recover_expired()

    assert outcome.new_status is TaskStatus.FAILED
    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == (TaskStatus.FAILED, 2, None)
    assert await leases.acquire(task_id, "engine-3/worker", TTL) is None
    attempts = await events_of(sessions, "recovery.attempted", task_id)
    assert [e.payload["success"] for e in attempts] == [True, False]
    assert attempts[-1].payload["reason"] == "failed: attempts exhausted"


async def test_recovery_is_idempotent_across_concurrent_sweeps(
    sessions, objective_id, clock
) -> None:
    """Two recovery sweeps racing over the same expired lease requeue it once."""
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)
    assert await leases.acquire(task_id, "engine-dead/worker", TTL) is not None
    clock.advance(TTL.total_seconds() + 1)

    first, second = await asyncio.gather(
        LeaseManager(sessions, clock=clock).recover_expired(),
        LeaseManager(sessions, clock=clock).recover_expired(),
    )

    assert len(first) + len(second) == 1
    assert (await event_counts(sessions, task_id))["task.lease_expired"] == 1
    assert await task_state(sessions, task_id) is TaskStatus.READY


async def test_hung_worker_loses_its_lease_and_cannot_record_a_second_outcome(
    sessions, objective_id
) -> None:
    """Engine A's worker hangs without renewing; engine B recovers and completes the task."""
    hang = asyncio.Event()

    async def hung(_task: TaskRecord) -> TaskResult:
        await hang.wait()
        return TaskResult(status=TaskResultStatus.COMPLETED, summary="late")

    stuck_agent = RecordingAgent(hung)
    stuck = build_engine(
        sessions,
        [stuck_agent],
        limits=engine_limits(
            lease_ttl_seconds=0.3,
            lease_renew_seconds=600.0,  # renewals never happen: the worker is wedged
            recovery_interval_seconds=600.0,
            max_tasks_per_agent=1,
        ),
    )
    task_id = await make_task(sessions, objective_id, max_attempts=5)
    healthy_agent = RecordingAgent()
    healthy = build_engine(sessions, [healthy_agent])

    await stuck.start()
    try:
        await wait_until(lambda: asyncio.sleep(0, bool(stuck_agent.executed)))
        await healthy.start()
        await wait_until(lambda: _in_state(sessions, task_id, TaskStatus.COMPLETED), timeout=10.0)
        hang.set()  # the wedged worker wakes up and tries to report its own result
        await wait_until(lambda: asyncio.sleep(0, stuck.pool.running_count == 0))
    finally:
        hang.set()
        await healthy.stop()
        await stuck.stop()

    assert stuck_agent.executed == [task_id]
    assert healthy_agent.executed == [task_id]
    task = await get_task(sessions, task_id)
    assert (task.state, task.attempt, task.lease_owner) == (TaskStatus.COMPLETED, 2, None)
    counts = await event_counts(sessions, task_id)
    assert counts["task.completed"] == 1
    assert counts["task.lease_expired"] == 1
    [completed] = await events_of(sessions, "task.completed", task_id)
    assert completed.actor == f"{healthy.pool.owner}/{task_id}"[:24]


async def _in_state(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state
