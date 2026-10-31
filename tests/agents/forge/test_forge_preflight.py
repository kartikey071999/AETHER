from datetime import timedelta
from uuid import uuid4

import pytest
from forge_support import ForgeEnv, py, step, write
from sqlalchemy import update

from aether.agents.base import TaskResult, TaskResultStatus
from aether.communication.contracts import ResourceEstimate
from aether.communication.messages import AgentId, MessageType
from aether.security import DecisionOutcome
from aether.storage.models import TaskRecord, TaskStatus
from aether.storage.repositories import TaskRepository


def codes(result: TaskResult) -> list[str]:
    return result.outputs["refusal_codes"]


async def assert_refused(env: ForgeEnv, task: TaskRecord, result: TaskResult, code: str) -> None:
    assert result.status is TaskResultStatus.BLOCKED
    assert result.retryable is False
    assert code in codes(result)
    assert not env.workspace(task).exists()
    assert not await env.messages(MessageType.EXECUTION_RESULT)
    assert not await env.messages(MessageType.VERIFICATION_REQUEST)
    blockers = await env.messages(MessageType.BLOCKER)
    assert blockers and code in blockers[0].payload["description"]


async def test_missing_plan_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "implementation_plan_missing")


async def test_missing_acceptance_criteria_is_refused(env: ForgeEnv) -> None:
    spec = '```forge-spec\n{"kind": "create_files", "files": {"a.py": "x = 1\\n"}}\n```'
    task = await env.make_task(criteria=None, description=spec)
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "acceptance_criteria_missing")


async def test_unknown_task_is_refused_without_publishing(env: ForgeEnv) -> None:
    task = await env.make_task()
    ghost = TaskRecord(id=uuid4(), objective_id=task.objective_id, title="ghost", description="")
    result = await env.agent.execute_task(ghost, env.ctx)
    assert result.status is TaskResultStatus.BLOCKED
    assert "task_missing" in codes(result)
    assert not await env.messages(MessageType.BLOCKER)


async def test_task_must_be_running_with_a_live_lease(env: ForgeEnv) -> None:
    task = await env.make_task(running=False)
    await env.deliver_plan(task, [write("a.py")])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "scheduler_state_not_running")


async def test_expired_lease_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write("a.py")])
    env.clock.advance(timedelta(hours=1))
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "no_live_lease")


async def test_task_assigned_to_another_agent_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task(assigned_agent="SCOUT")
    await env.deliver_plan(task, [write("a.py")])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "not_assigned_to_forge")


async def test_incomplete_dependency_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    other = await env.make_task(running=False)
    await env.deliver_plan(task, [write("a.py")], depends_on=[other.id])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "dependency_incomplete")


async def test_completed_dependency_clears(env: ForgeEnv) -> None:
    task = await env.make_task()
    other = await env.make_task(running=False)
    repository = TaskRepository(env.database.sessions)
    await repository.transition(other.id, TaskStatus.READY, actor="test")
    await repository.transition(other.id, TaskStatus.SCHEDULED, actor="test")
    await repository.transition(other.id, TaskStatus.RUNNING, actor="test")
    await repository.transition(other.id, TaskStatus.COMPLETED, actor="test")
    await env.deliver_plan(task, [write("a.py")], depends_on=[other.id])
    result = await env.agent.execute_task(task, env.ctx)
    assert result.status is TaskResultStatus.COMPLETED


async def test_missing_resource_estimate_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write("a.py")], estimate=ResourceEstimate())
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "resource_estimate_missing")


async def test_estimate_over_the_limits_is_refused_as_a_resource_blocker(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write("a.py")], estimate=ResourceEstimate(ram_mb=10**6))
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "resource_estimate_exceeds_limits")
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.payload["kind"] == "resource"


async def test_unregistered_tool_is_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [step("format_disk", "do harm")])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "unregistered_tool")


@pytest.mark.parametrize("path", ["../escape.py", "/etc/passwd", "C:/Windows/evil.py"])
async def test_paths_escaping_the_workspace_are_refused(env: ForgeEnv, path: str) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write(path)])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "implementation_plan_invalid")


async def test_too_many_steps_are_refused(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write(f"f{n}.py") for n in range(21)])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "too_many_steps")


async def test_rejected_requirement_is_refused(env: ForgeEnv) -> None:
    from aether.storage.models import RequirementRecord

    task = await env.make_task()
    async with env.database.sessions() as session, session.begin():
        await session.execute(
            update(RequirementRecord)
            .where(RequirementRecord.id == task.requirement_id)
            .values(status="rejected")
        )
    await env.deliver_plan(task, [write("a.py")])
    result = await env.agent.execute_task(task, env.ctx)
    await assert_refused(env, task, result, "requirement_inactive")


async def test_unreviewed_subprocess_waits_for_warden_without_executing(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write("a.py"), py("open('ran', 'w')")])

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.WAITING_FOR_APPROVAL
    assert not env.workspace(task).exists()
    [pending] = result.outputs["pending_operations"]
    assert pending["kind"] == "subprocess"
    [review] = await env.messages(MessageType.SECURITY_EVALUATE)
    assert review.target_agent == AgentId.WARDEN.value
    assert review.payload["input_hash"] == pending["input_hash"]
    assert review.payload["requested_by"] == AgentId.FORGE.value

    again = await env.agent.execute_task(task, env.ctx)
    assert again.status is TaskResultStatus.WAITING_FOR_APPROVAL
    assert len(await env.messages(MessageType.SECURITY_EVALUATE)) == 1


async def test_warden_approval_unblocks_execution(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [py("open('ran', 'w')")])
    assert (await env.agent.execute_task(task, env.ctx)).status is (
        TaskResultStatus.WAITING_FOR_APPROVAL
    )

    await env.approve_plan(task)
    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert (env.workspace(task) / "ran").exists()


async def test_expired_approval_is_requested_again(env: ForgeEnv) -> None:
    task = await env.make_task(lease_ttl=timedelta(hours=5))
    await env.deliver_plan(task, [py("print(1)")])
    await env.approve_plan(task)
    env.clock.advance(timedelta(hours=2))

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.WAITING_FOR_APPROVAL
    assert result.outputs["pending_operations"][0]["reason"] == "the approval has expired"


async def test_rejected_operation_is_refused_and_routed_to_warden(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [py("print(1)")])
    await env.approve_plan(task, decision=DecisionOutcome.REJECTED)

    result = await env.agent.execute_task(task, env.ctx)

    await assert_refused(env, task, result, "security_decision_rejected")
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.payload["kind"] == "security"
    assert blocker.payload["route_to"] == AgentId.WARDEN.value
