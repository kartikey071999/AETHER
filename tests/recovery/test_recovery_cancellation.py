"""Scenario 6: cancellation is durable, prompt and never undone by a crash or restart.

A cancel request is stored as an event before it is applied, so it survives the death of the
worker that should have honoured it: once the dead lease is recovered the next lease cancels
the task without running the agent again.
"""

import asyncio
from datetime import timedelta

import pytest
from reliability_support import (
    RecordingAgent,
    build_engine,
    event_counts,
    events_of,
    get_task,
    make_task,
    task_state,
    wait_until,
)

from aether.agents.base import TaskResult
from aether.runtime.control import TaskControl
from aether.runtime.leases import LeaseManager
from aether.storage.models import TaskRecord, TaskStatus


@pytest.mark.parametrize(
    "state", [TaskStatus.PENDING, TaskStatus.READY, TaskStatus.WAITING_FOR_APPROVAL]
)
async def test_cancelling_a_task_that_is_not_running_applies_immediately(
    sessions, objective_id, state: TaskStatus
) -> None:
    task_id = await make_task(sessions, objective_id, state=state)

    result = await TaskControl(sessions).request_cancel(task_id, reason="operator stop")

    assert (result.outcome, result.state) == ("applied", TaskStatus.CANCELLED)
    assert await task_state(sessions, task_id) is TaskStatus.CANCELLED
    counts = await event_counts(sessions, task_id)
    assert counts["task.control_requested"] == counts["task.control_resolved"] == 1


async def test_cancelled_tasks_are_never_leased_after_a_restart(sessions, objective_id) -> None:
    task_id = await make_task(sessions, objective_id)
    await TaskControl(sessions).request_cancel(task_id)
    agent = RecordingAgent()
    engine = build_engine(sessions, [agent])

    await engine.start()
    try:
        await asyncio.sleep(0.3)
    finally:
        await engine.stop()

    assert agent.executed == []
    assert await task_state(sessions, task_id) is TaskStatus.CANCELLED


async def test_cancelling_a_running_task_interrupts_its_agent(sessions, objective_id) -> None:
    started, interrupted = asyncio.Event(), asyncio.Event()

    async def long_running(_task: TaskRecord) -> TaskResult:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            raise
        raise AssertionError("unreachable")

    agent = RecordingAgent(long_running)
    engine = build_engine(sessions, [agent])
    task_id = await make_task(sessions, objective_id)

    await engine.start()
    try:
        await asyncio.wait_for(started.wait(), 5.0)
        result = await engine.task_control.request_cancel(task_id, reason="operator stop")
        assert result.outcome == "pending"  # the worker owning the lease applies it
        await asyncio.wait_for(interrupted.wait(), 5.0)
        await wait_until(lambda: _is(sessions, task_id, TaskStatus.CANCELLED))
    finally:
        await engine.stop()

    task = await get_task(sessions, task_id)
    assert (task.lease_owner, task.attempt) == (None, 1)
    counts = await event_counts(sessions, task_id)
    assert counts["task.cancelled"] == 1
    assert counts["task.control_resolved"] == 1
    assert "task.completed" not in counts and "task.failed" not in counts


async def test_cancel_requested_for_a_crashed_worker_is_honoured_after_recovery(
    sessions, objective_id
) -> None:
    task_id = await make_task(sessions, objective_id)
    dead = await LeaseManager(sessions).acquire(
        task_id, "engine-dead/worker", timedelta(seconds=0.2)
    )
    assert dead is not None

    # The request lands while the (already dead) worker still holds the lease.
    result = await TaskControl(sessions).request_cancel(task_id, reason="operator stop")
    assert (result.outcome, result.state) == ("pending", TaskStatus.RUNNING)
    await asyncio.sleep(0.25)  # the dead lease expires

    agent = RecordingAgent()
    engine = build_engine(sessions, [agent])
    await engine.start()
    try:
        await wait_until(lambda: _is(sessions, task_id, TaskStatus.CANCELLED))
    finally:
        await engine.stop()

    assert agent.executed == []  # the cancel was applied before the agent could run again
    counts = await event_counts(sessions, task_id)
    assert counts["task.lease_expired"] == 1
    assert counts["task.cancelled"] == 1
    [resolved] = await events_of(sessions, "task.control_resolved", task_id)
    assert resolved.payload["outcome"] == TaskStatus.CANCELLED.value


async def test_cancel_after_completion_is_a_noop(sessions, objective_id) -> None:
    task_id = await make_task(sessions, objective_id, state=TaskStatus.COMPLETED)
    control = TaskControl(sessions)

    first = await control.request_cancel(task_id)
    second = await control.request_cancel(task_id)

    assert (first.outcome, second.outcome) == ("noop", "noop")
    assert await task_state(sessions, task_id) is TaskStatus.COMPLETED
    assert "task.control_requested" not in await event_counts(sessions, task_id)


async def test_repeated_cancel_requests_cancel_once(sessions, objective_id) -> None:
    task_id = await make_task(sessions, objective_id)
    control = TaskControl(sessions)

    results = await asyncio.gather(*(control.request_cancel(task_id) for _ in range(3)))

    assert await task_state(sessions, task_id) is TaskStatus.CANCELLED
    assert sum(result.outcome == "applied" for result in results) == 1
    transitions = await events_of(sessions, "task.transitioned", task_id)
    assert [e.payload["to_status"] for e in transitions] == [TaskStatus.CANCELLED.value]


async def _is(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state
