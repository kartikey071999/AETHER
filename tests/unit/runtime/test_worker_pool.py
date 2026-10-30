import asyncio
from datetime import timedelta

import pytest
from runtime_helpers import StubAgent, event_counts, get_task, make_task, stub_for, wait_until

from aether.agents.base import AgentContext, TaskResult, TaskResultStatus
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.runtime.leases import LeaseManager
from aether.runtime.worker import WorkerPool
from aether.storage.models import TaskRecord, TaskStatus


def build_pool(sessions, **overrides) -> WorkerPool:
    context = AgentContext(sessions=sessions, bus=MessageBus(sessions))
    options = {
        "max_concurrent": 2,
        "task_timeout": 5.0,
        "lease_ttl": timedelta(seconds=30),
        "renew_interval": 0.05,
        "owner": "pool",
    }
    options.update(overrides)
    return WorkerPool(LeaseManager(sessions), context, **options)


class Gate:
    """Behaviour that blocks until released, so tests control task lifetimes."""

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def __call__(self, _task: TaskRecord) -> TaskResult:
        await self.event.wait()
        return TaskResult(status=TaskResultStatus.COMPLETED)


async def test_successful_task_is_completed_with_events(sessions, objective_id) -> None:
    pool = build_pool(sessions)
    agent = StubAgent()
    task_id = await make_task(sessions, objective_id)

    assert await pool.submit(task_id, agent) is True
    await pool.join()

    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.COMPLETED
    assert task.lease_owner is None and task.attempt == 1
    counts = await event_counts(sessions, task_id)
    assert counts["task.completed"] == 1 and counts["task.lease_released"] == 1
    assert pool.running_count == 0


async def test_global_concurrency_limit_is_enforced(sessions, objective_id) -> None:
    gate = Gate()
    pool = build_pool(sessions, max_concurrent=2)
    agent = StubAgent(gate)
    ids = [await make_task(sessions, objective_id) for _ in range(3)]

    assert await pool.submit(ids[0], agent) is True
    assert await pool.submit(ids[1], agent) is True
    assert pool.has_capacity(AgentId.FORGE) is False
    assert await pool.submit(ids[2], agent) is False
    assert (await get_task(sessions, ids[2])).state == TaskStatus.READY

    await wait_until(lambda: asyncio.sleep(0, len(agent.started_tasks) == 2))
    gate.event.set()
    await pool.join()
    assert agent.max_active == 2
    assert await pool.submit(ids[2], agent) is True
    await pool.join()
    assert (await get_task(sessions, ids[2])).state == TaskStatus.COMPLETED


async def test_per_agent_limit_does_not_block_other_agents(sessions, objective_id) -> None:
    gate = Gate()
    pool = build_pool(sessions, max_concurrent=3, per_agent_limit={AgentId.FORGE: 1})
    forge = stub_for(AgentId.FORGE, gate)
    scout = stub_for(AgentId.SCOUT, gate)
    first = await make_task(sessions, objective_id)
    second = await make_task(sessions, objective_id)
    other = await make_task(sessions, objective_id, agent="SCOUT")

    assert await pool.submit(first, forge) is True
    assert await pool.submit(second, forge) is False
    assert await pool.submit(other, scout) is True
    assert pool.running_count == 2

    gate.event.set()
    await pool.join()


async def test_failed_lease_does_not_leak_capacity(sessions, objective_id) -> None:
    pool = build_pool(sessions, max_concurrent=1)
    agent = StubAgent()
    pending = await make_task(sessions, objective_id, state=TaskStatus.PENDING)
    ready = await make_task(sessions, objective_id)

    assert await pool.submit(pending, agent) is False
    assert pool.has_capacity(AgentId.FORGE) is True
    assert await pool.submit(ready, agent) is True
    await pool.join()


async def test_timeout_retries_then_fails_when_attempts_exhausted(sessions, objective_id) -> None:
    async def hang(_task: TaskRecord) -> TaskResult:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    pool = build_pool(sessions, task_timeout=0.05)
    agent = StubAgent(hang)
    retryable = await make_task(sessions, objective_id, max_attempts=3)
    final = await make_task(sessions, objective_id, max_attempts=1)

    await pool.submit(retryable, agent)
    await pool.submit(final, agent)
    await pool.join()

    assert (await get_task(sessions, retryable)).state == TaskStatus.READY
    assert (await get_task(sessions, final)).state == TaskStatus.FAILED
    assert (await event_counts(sessions, retryable))["task.timed_out"] == 1
    assert (await event_counts(sessions, final))["task.timed_out"] == 1
    assert agent.active == 0


async def test_exception_and_reported_failure_are_recorded(sessions, objective_id) -> None:
    async def explode(_task: TaskRecord) -> TaskResult:
        raise RuntimeError("kaboom")

    async def give_up(_task: TaskRecord) -> TaskResult:
        return TaskResult(status=TaskResultStatus.FAILED, error="no way", retryable=False)

    pool = build_pool(sessions)
    crashed = await make_task(sessions, objective_id)
    refused = await make_task(sessions, objective_id)

    await pool.submit(crashed, StubAgent(explode))
    await pool.submit(refused, StubAgent(give_up))
    await pool.join()

    assert (await get_task(sessions, crashed)).state == TaskStatus.READY
    assert (await get_task(sessions, refused)).state == TaskStatus.FAILED
    assert (await event_counts(sessions, crashed))["task.failed"] == 1


async def test_blocked_and_approval_results_map_to_task_states(sessions, objective_id) -> None:
    async def blocked(_task: TaskRecord) -> TaskResult:
        return TaskResult(status=TaskResultStatus.BLOCKED, summary="needs input")

    async def approval(_task: TaskRecord) -> TaskResult:
        return TaskResult(status=TaskResultStatus.WAITING_FOR_APPROVAL)

    pool = build_pool(sessions)
    first = await make_task(sessions, objective_id)
    second = await make_task(sessions, objective_id)

    await pool.submit(first, StubAgent(blocked))
    await pool.submit(second, StubAgent(approval))
    await pool.join()

    assert (await get_task(sessions, first)).state == TaskStatus.BLOCKED
    assert (await get_task(sessions, second)).state == TaskStatus.WAITING_FOR_APPROVAL


async def test_cancel_stops_running_task_and_records_cancellation(sessions, objective_id) -> None:
    pool = build_pool(sessions)
    agent = StubAgent(Gate())
    task_id = await make_task(sessions, objective_id)
    await pool.submit(task_id, agent)

    assert await pool.cancel(task_id) is True
    assert await pool.cancel(task_id) is False

    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.CANCELLED and task.lease_owner is None
    assert agent.is_cancelled(task_id)
    assert (await event_counts(sessions, task_id))["task.cancelled"] == 1
    assert pool.running_count == 0 and pool.has_capacity(AgentId.FORGE)


async def test_shutdown_requeues_running_work_without_spending_an_attempt(
    sessions, objective_id
) -> None:
    pool = build_pool(sessions)
    task_id = await make_task(sessions, objective_id)
    await pool.submit(task_id, StubAgent(Gate()))

    await pool.shutdown(0.05)

    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.READY and task.attempt == 0 and task.lease_owner is None
    assert (await event_counts(sessions, task_id))["task.interrupted"] == 1
    assert await pool.submit(task_id, StubAgent()) is False  # no longer accepting


async def test_shutdown_lets_quick_tasks_finish_within_grace(sessions, objective_id) -> None:
    async def short(_task: TaskRecord) -> TaskResult:
        await asyncio.sleep(0.05)
        return TaskResult(status=TaskResultStatus.COMPLETED)

    pool = build_pool(sessions)
    task_id = await make_task(sessions, objective_id)
    await pool.submit(task_id, StubAgent(short))

    await pool.shutdown(2.0)

    assert (await get_task(sessions, task_id)).state == TaskStatus.COMPLETED


async def test_lease_is_renewed_while_task_runs(sessions, objective_id) -> None:
    gate = Gate()
    pool = build_pool(sessions, lease_ttl=timedelta(seconds=0.5), renew_interval=0.05)
    leases = LeaseManager(sessions)
    task_id = await make_task(sessions, objective_id)
    await pool.submit(task_id, StubAgent(gate))

    await asyncio.sleep(1.1)  # more than twice the TTL
    assert await leases.recover_expired() == []
    assert (await get_task(sessions, task_id)).state == TaskStatus.RUNNING

    gate.event.set()
    await pool.join()
    assert (await get_task(sessions, task_id)).state == TaskStatus.COMPLETED


async def test_lost_lease_stops_the_task_without_overwriting_new_owner(
    sessions, objective_id
) -> None:
    gate = Gate()
    pool = build_pool(sessions, lease_ttl=timedelta(seconds=30), renew_interval=0.02)
    leases = LeaseManager(sessions)
    task_id = await make_task(sessions, objective_id)
    agent = StubAgent(gate)
    await pool.submit(task_id, agent)

    # Simulate a sweeper that decided the lease was dead and handed the task to someone else.
    owner = (await get_task(sessions, task_id)).lease_owner
    assert owner is not None
    await leases.release(task_id, owner, TaskStatus.READY, refund_attempt=True)
    assert await leases.acquire(task_id, "other", timedelta(seconds=30)) is not None

    await pool.join()

    task = await get_task(sessions, task_id)
    assert task.state == TaskStatus.RUNNING and task.lease_owner == "other"
    assert agent.active == 0


async def test_invalid_pool_size_is_rejected(sessions) -> None:
    with pytest.raises(ValueError):
        build_pool(sessions, max_concurrent=0)
