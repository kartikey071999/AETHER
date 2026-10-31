import sys
from uuid import UUID

from forge_support import CRITERIA, HELLO, ForgeEnv, py, write

from aether.agents.base import TaskResultStatus
from aether.communication.messages import AgentId, MessageType


async def test_happy_path_executes_records_evidence_and_requests_verification(
    env: ForgeEnv,
) -> None:
    task = await env.make_task()
    await env.deliver_plan(
        task,
        [write("hello.py"), py("import hello", "import the module")],
        [py("print('rollback')")],
    )
    await env.approve_plan(task)

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.COMPLETED, result
    assert (env.workspace(task) / "hello.py").read_text() == HELLO
    assert result.outputs["verified"] is False
    assert result.outputs["deployed"] is False
    process = [item for item in result.evidence if item["type"] == "process"]
    assert process[0]["exit_code"] == 0
    assert "hello from forge" in process[0]["stdout"]
    assert process[0]["command"].startswith(sys.executable)
    written = [item for item in result.evidence if item["type"] == "write"]
    assert any(item["target"].endswith("hello.py") and item["sha256"] for item in written)

    results = await env.messages(MessageType.EXECUTION_RESULT)
    assert len(results) == 1
    assert results[0].target_agent == AgentId.LEDGER.value
    assert results[0].payload["status"] == "succeeded"
    assert [UUID(item) for item in results[0].payload["execution_run_ids"]]

    requests = await env.messages(MessageType.VERIFICATION_REQUEST)
    assert len(requests) == 1
    assert requests[0].payload["acceptance_criteria"] == CRITERIA
    assert requests[0].payload["requirement_id"] == str(task.requirement_id)
    assert not await env.messages(MessageType.CAPABILITY_REGISTER)
    assert not await env.messages(MessageType.BLOCKER)


async def test_execution_stays_inside_the_task_workspace(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [write("pkg/mod.py")])
    result = await env.agent.execute_task(task, env.ctx)
    assert result.status is TaskResultStatus.COMPLETED
    assert (env.workspace(task) / "pkg" / "mod.py").exists()
    assert (env.workspace(task) / ".forge" / "manifest.json").exists()


async def test_redelivery_after_completion_replays_without_rerunning(env: ForgeEnv) -> None:
    task = await env.make_task()
    marker = "open('ran.txt', 'a').write('x')"
    await env.deliver_plan(task, [py(marker)])
    await env.approve_plan(task)

    first = await env.agent.execute_task(task, env.ctx)
    second = await env.agent.execute_task(await env.task(task.id), env.ctx)

    assert first.status is second.status is TaskResultStatus.COMPLETED
    assert (env.workspace(task) / "ran.txt").read_text() == "x"
    assert len(await env.messages(MessageType.EXECUTION_RESULT)) == 1
    assert len(await env.messages(MessageType.VERIFICATION_REQUEST)) == 1
