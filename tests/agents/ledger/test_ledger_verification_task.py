"""LEDGER's planned verification task waits for the real test run (T-042)."""

from uuid import UUID

from test_ledger_verification import PASSING_SUITE, run_verification, write_suite

from aether.agents.base import TaskResultStatus
from aether.communication.contracts import VerificationVerdict
from aether.storage.models import TaskRecord, TaskStatus
from aether.storage.repositories import TaskDependencyRepository, TaskRepository


async def _verification_task(database, work, *, requirement: bool = True) -> UUID:
    tasks = TaskRepository(database.sessions)
    task_id = await tasks.create(
        objective_id=work.objective_id,
        title="Verify independently",
        assigned_agent="LEDGER",
        requirement_id=work.requirement_id if requirement else None,
    )
    await TaskDependencyRepository(database.sessions).add(task_id, work.task_id, actor="test")
    for step in (TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING):
        await tasks.transition(task_id, step, actor="test")
    return task_id


async def _task(database, task_id: UUID) -> TaskRecord:
    async with database.sessions() as session:
        task = await session.get(TaskRecord, task_id, populate_existing=True)
    assert task is not None
    return task


async def test_task_waits_without_a_test_run(agent, ctx, database, make_work):
    work = await make_work()
    task_id = await _verification_task(database, work)

    result = await agent.execute_task(await _task(database, task_id), ctx)

    assert result.status is TaskResultStatus.WAITING_FOR_APPROVAL
    assert result.outputs["awaiting_test_run_for"] == [str(work.task_id)]


async def test_task_completes_once_the_run_has_a_verdict(
    agent, ctx, publish, approve_pending, database, make_work, workspace
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    task_id = await _verification_task(database, work)
    tasks = TaskRepository(database.sessions)
    await tasks.transition(task_id, TaskStatus.WAITING_FOR_APPROVAL, actor="test")

    assert await agent.tick(ctx) == []
    assert (await _task(database, task_id)).state == TaskStatus.WAITING_FOR_APPROVAL.value

    run = await run_verification(agent, ctx, publish, approve_pending, work)

    # Finishing the run released the waiting task; the tick is a no-op afterwards.
    assert run.verdict == VerificationVerdict.VERIFIED.value
    assert (await _task(database, task_id)).state == TaskStatus.READY.value
    assert await agent.tick(ctx) == []

    for step in (TaskStatus.SCHEDULED, TaskStatus.RUNNING):
        await tasks.transition(task_id, step, actor="test")
    result = await agent.execute_task(await _task(database, task_id), ctx)
    assert result.status is TaskResultStatus.COMPLETED
    assert result.outputs["test_run_id"] == str(run.id)
    assert result.outputs["verdict"] == VerificationVerdict.VERIFIED.value


async def test_tick_resumes_a_task_whose_run_finished_before_it_waited(
    agent, ctx, publish, approve_pending, database, make_work, workspace
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    await run_verification(agent, ctx, publish, approve_pending, work)
    task_id = await _verification_task(database, work)
    tasks = TaskRepository(database.sessions)
    await tasks.transition(task_id, TaskStatus.WAITING_FOR_APPROVAL, actor="test")

    assert await agent.tick(ctx) == [task_id]
    assert (await _task(database, task_id)).state == TaskStatus.READY.value


async def test_tasks_without_forge_work_or_requirement_keep_completing(
    agent, ctx, database, make_work
):
    work = await make_work()
    unlinked = await _verification_task(database, work, requirement=False)
    standalone = await TaskRepository(database.sessions).create(
        objective_id=work.objective_id, title="Report", assigned_agent="LEDGER"
    )

    first = await agent.execute_task(await _task(database, unlinked), ctx)
    second = await agent.execute_task(await _task(database, standalone), ctx)

    assert first.status is TaskResultStatus.COMPLETED
    assert second.status is TaskResultStatus.COMPLETED
    assert "snapshot" in second.outputs
