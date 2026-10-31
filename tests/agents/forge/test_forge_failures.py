from collections.abc import Callable
from uuid import uuid4

import pytest
from forge_support import ForgeEnv, py, step, write

from aether.agents.base import TaskResultStatus
from aether.agents.forge.schemas import ForgeConfig
from aether.agents.forge.store import Journal
from aether.communication.messages import AgentId, MessageType


def fail(stderr: str, code: int = 1) -> str:
    return f"import sys; sys.stderr.write({stderr!r}); sys.exit({code})"


async def run_failing(env: ForgeEnv, stderr: str) -> tuple:
    task = await env.make_task()
    await env.deliver_plan(
        task,
        [write("work.py"), py(fail(stderr), "the failing step"), write("never.py")],
        [py("import os; os.remove('work.py')", "undo the write")],
    )
    await env.approve_plan(task)
    result = await env.agent.execute_task(task, env.ctx)
    return task, result


@pytest.mark.parametrize(
    ("stderr", "failure_class", "route", "kind"),
    [
        ("AssertionError: 1 != 2", "code_defect", AgentId.ORCHESTR, "implementation"),
        (
            "ModuleNotFoundError: No module named 'yaml'",
            "missing_dependency",
            AgentId.SCOUT,
            "research",
        ),
        ("something odd happened", "unknown", AgentId.SCOUT, "research"),
    ],
)
async def test_failure_rolls_back_and_routes_a_blocker(
    env: ForgeEnv, stderr: str, failure_class: str, route: AgentId, kind: str
) -> None:
    task, result = await run_failing(env, stderr)

    assert result.status is TaskResultStatus.BLOCKED
    assert result.retryable is False
    assert result.outputs["failure_class"] == failure_class
    assert result.outputs["failed_step"] == "steps[1]"
    assert result.outputs["rollback"] == {"executed": 1, "failed": 0}
    assert not (env.workspace(task) / "work.py").exists()
    assert not (env.workspace(task) / "never.py").exists()
    assert env.sleeps == []

    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.target_agent == route.value
    assert blocker.payload["kind"] == kind
    assert blocker.payload["task_id"] == str(task.id)
    assert "steps[1]" in blocker.payload["description"]

    [outcome] = await env.messages(MessageType.EXECUTION_RESULT)
    assert outcome.payload["status"] == "failed"
    assert outcome.payload["error"]
    assert not await env.messages(MessageType.VERIFICATION_REQUEST)


async def test_environment_failures_are_retried_with_bounded_backoff(env: ForgeEnv) -> None:
    task, result = await run_failing(env, "ConnectionError: connection refused")

    assert result.status is TaskResultStatus.BLOCKED
    assert result.outputs["failure_class"] == "environment"
    assert env.sleeps == [1.0, 2.0]
    entries = await env.journal(task.id)
    assert sum(1 for e in entries if e["phase"] == "steps" and e["status"] == "failed") == 3
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.payload["kind"] == "resource"
    assert blocker.payload["attempts"] == 3


async def test_transient_environment_failure_recovers_on_retry(env: ForgeEnv) -> None:
    task = await env.make_task()
    flaky = (
        "import os, sys\n"
        "if not os.path.exists('seen'):\n"
        "    open('seen', 'w').close()\n"
        "    sys.stderr.write('ConnectionError: refused')\n"
        "    sys.exit(1)\n"
        "print('ok')"
    )
    await env.deliver_plan(task, [py(flaky)])
    await env.approve_plan(task)

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert env.sleeps == [1.0]


async def test_timeout_is_an_environment_failure(make_env: Callable[..., ForgeEnv]) -> None:
    env = make_env(ForgeConfig(step_timeout_seconds=1, max_step_retries=0))
    task = await env.make_task()
    await env.deliver_plan(task, [py("import time; time.sleep(30)")])
    await env.approve_plan(task)

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.BLOCKED
    assert result.outputs["failure_class"] == "environment"


async def test_retry_budget_is_counted_across_restarts(
    make_env: Callable[..., ForgeEnv],
) -> None:
    env = make_env(ForgeConfig(max_step_retries=1))
    task = await env.make_task()
    await env.deliver_plan(task, [py(fail("ConnectionError: down"))])
    await env.approve_plan(task)
    first = await env.agent.execute_task(task, env.ctx)
    second = await env.agent.execute_task(task, env.ctx)

    assert first.status is second.status is TaskResultStatus.BLOCKED
    assert len(await env.messages(MessageType.BLOCKER)) == 1
    entries = await env.journal(task.id)
    assert sum(1 for e in entries if e["phase"] == "steps" and e["status"] == "failed") == 2


async def test_non_allowlisted_executable_is_blocked_and_reported_as_security(
    env: ForgeEnv,
) -> None:
    task = await env.make_task()
    await env.deliver_plan(
        task, [step("run_process", "run a shell", argv=["cmd.exe", "/c", "dir"])]
    )
    await env.approve_plan(task)

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.BLOCKED
    assert result.outputs["failure_class"] == "security"
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.payload["kind"] == "security"
    assert blocker.payload["route_to"] == AgentId.WARDEN.value
    assert blocker.target_agent == AgentId.ORCHESTR.value


async def test_restart_mid_plan_resumes_from_the_journal(env: ForgeEnv) -> None:
    task = await env.make_task()
    first = "open('one.txt', 'a').write('1')"
    second = "open('two.txt', 'a').write('2')"
    plan_id = await env.deliver_plan(task, [py(first, "first"), py(second, "second")])
    await env.approve_plan(task)
    journal = await Journal(env.ctx, task, plan_id).load()
    await journal.append(
        "steps", 0, "succeeded", evidence={"type": "process", "run_id": str(uuid4())}
    )

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert not (env.workspace(task) / "one.txt").exists()
    assert (env.workspace(task) / "two.txt").read_text() == "2"


async def test_cancelled_task_stops_before_the_next_step(env: ForgeEnv) -> None:
    task = await env.make_task()
    await env.deliver_plan(task, [py("print(1)")])
    await env.approve_plan(task)
    await env.agent.cancel(task.id)

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.FAILED
    assert result.error == "cancelled"
    assert not await env.messages(MessageType.EXECUTION_RESULT)
