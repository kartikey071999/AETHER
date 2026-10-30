import asyncio
from datetime import timedelta

from runtime_helpers import event_counts, get_task, make_task

from aether.runtime.leases import LeaseManager
from aether.storage.models import TaskStatus

TTL = timedelta(seconds=30)


async def test_acquire_moves_ready_task_to_running(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)

    lease = await leases.acquire(task_id, "w1", TTL)

    assert lease is not None and lease.attempt == 1
    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.RUNNING
    assert task.lease_owner == "w1"
    assert task.attempt == 1
    counts = await event_counts(sessions, task_id)
    assert counts["task.lease_acquired"] == 1
    assert counts["task.transitioned"] == 2  # ready->scheduled->running


async def test_acquire_rejects_non_leasable_and_already_leased_tasks(
    sessions, objective_id, clock
) -> None:
    leases = LeaseManager(sessions, clock=clock)
    pending = await make_task(sessions, objective_id, state=TaskStatus.PENDING)
    ready = await make_task(sessions, objective_id)

    assert await leases.acquire(pending, "w1", TTL) is None
    assert await leases.acquire(ready, "w1", TTL) is not None
    assert await leases.acquire(ready, "w2", TTL) is None


async def test_concurrent_acquirers_yield_exactly_one_owner(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)

    results = await asyncio.gather(*(leases.acquire(task_id, f"w{i}", TTL) for i in range(8)))

    winners = [lease for lease in results if lease is not None]
    assert len(winners) == 1
    task = await get_task(sessions, task_id)
    assert task.lease_owner == winners[0].owner
    assert task.attempt == 1


async def test_renew_extends_only_for_owner_and_live_lease(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)
    await leases.acquire(task_id, "w1", TTL)

    clock.advance(20)
    assert await leases.renew(task_id, "w2", TTL) is False
    assert await leases.renew(task_id, "w1", TTL) is True
    clock.advance(20)  # would have expired without the renewal
    assert await leases.recover_expired() == []
    clock.advance(11)
    assert await leases.renew(task_id, "w1", TTL) is False


async def test_release_sets_final_status_and_clears_lease(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)
    await leases.acquire(task_id, "w1", TTL)

    assert await leases.release(task_id, "w2", TaskStatus.COMPLETED) is False
    released = await leases.release(
        task_id, "w1", TaskStatus.COMPLETED, outcome_event="task.completed"
    )
    assert released is True

    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.COMPLETED
    assert task.lease_owner is None and task.lease_expires_at is None
    counts = await event_counts(sessions, task_id)
    assert counts["task.lease_released"] == 1 and counts["task.completed"] == 1


async def test_release_back_to_ready_and_cancel_follow_transition_graph(
    sessions, objective_id, clock
) -> None:
    leases = LeaseManager(sessions, clock=clock)
    requeued = await make_task(sessions, objective_id)
    cancelled = await make_task(sessions, objective_id)
    await leases.acquire(requeued, "w1", TTL)
    await leases.acquire(cancelled, "w2", TTL)

    await leases.release(requeued, "w1", TaskStatus.READY, refund_attempt=True)
    await leases.release(cancelled, "w2", TaskStatus.CANCELLED)

    ready = await get_task(sessions, requeued)
    assert ready.state == TaskStatus.READY and ready.attempt == 0
    assert (await get_task(sessions, cancelled)).state == TaskStatus.CANCELLED
    assert await leases.acquire(requeued, "w3", TTL) is not None


async def test_recover_expired_requeues_with_attempts_left(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id, max_attempts=3)
    await leases.acquire(task_id, "dead", TTL)

    assert await leases.recover_expired() == []
    clock.advance(31)
    outcomes = await leases.recover_expired()

    assert [(o.task_id, o.new_status) for o in outcomes] == [(task_id, TaskStatus.READY)]
    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.READY and task.lease_owner is None and task.attempt == 1
    counts = await event_counts(sessions, task_id)
    assert counts["task.lease_expired"] == 1 and counts["recovery.attempted"] == 1
    assert await leases.acquire(task_id, "next", TTL) is not None


async def test_recover_expired_fails_task_at_max_attempts(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id, max_attempts=1)
    await leases.acquire(task_id, "dead", TTL)
    clock.advance(31)

    outcomes = await leases.recover_expired()

    assert [o.new_status for o in outcomes] == [TaskStatus.FAILED]
    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.FAILED and task.lease_owner is None
    assert await leases.acquire(task_id, "next", TTL) is None


async def test_recovery_is_idempotent(sessions, objective_id, clock) -> None:
    leases = LeaseManager(sessions, clock=clock)
    task_id = await make_task(sessions, objective_id)
    await leases.acquire(task_id, "dead", TTL)
    clock.advance(31)

    first, second = await asyncio.gather(leases.recover_expired(), leases.recover_expired())

    assert len(first) + len(second) == 1
    assert (await event_counts(sessions, task_id))["recovery.attempted"] == 1
