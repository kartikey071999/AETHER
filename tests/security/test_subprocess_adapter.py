import sys
import time
from pathlib import Path

import psutil
import pytest
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.security import BlockReason, OperationBlocked, OperationKind
from aether.storage.models import ExecutionRunRecord, RunStatus
from aether.tools import ToolInputError, ToolRegistry
from aether.tools.adapters import SafeSubprocess, scrub_environment

CHILD_TREE_SCRIPT = """
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pathlib.Path("child.pid").write_text(str(child.pid))
time.sleep(120)
"""


async def _approved_run(registry: ToolRegistry, approve, args: dict, agent=AgentId.FORGE):
    operation = registry.build_operation(agent, "run_process", args)
    await approve(operation)
    return await registry.invoke(agent, "run_process", args)


def _gone(pid: int, deadline: float = 10.0) -> bool:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.1)
    return False


async def test_approved_subprocess_runs_and_is_recorded(
    registry, approve, database, events_of
) -> None:
    result = await _approved_run(
        registry, approve, {"argv": [sys.executable, "-c", "print('hello from child')"]}
    )
    assert result.succeeded
    assert "hello from child" in result.stdout.content
    assert result.stdout.source.endswith(":stdout")
    assert result.stdout_path.read_text().strip() == "hello from child"
    async with database.sessions() as session:
        run = await session.get(ExecutionRunRecord, result.run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED
    assert run.exit_code == 0
    assert run.agent == "FORGE"
    assert run.security_decision_id is not None
    assert len(await events_of("execution.started")) == 1
    finished = await events_of("execution.finished")
    assert finished[0].payload["success"] is True


async def test_unapproved_subprocess_never_starts(registry, database) -> None:
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.FORGE, "run_process", {"argv": [sys.executable, "-V"]})
    assert raised.value.reason is BlockReason.APPROVAL_MISSING
    async with database.sessions() as session:
        assert list(await session.scalars(select(ExecutionRunRecord))) == []


async def test_non_allowlisted_executable_is_refused(registry, approve) -> None:
    args = {"argv": ["cmd" if sys.platform == "win32" else "sh", "-c", "echo pwned"]}
    operation = registry.build_operation(AgentId.FORGE, "run_process", args)
    await approve(operation)
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.FORGE, "run_process", args)
    assert raised.value.reason is BlockReason.POLICY_VIOLATION


async def test_cwd_traversal_is_refused(registry, approve) -> None:
    args = {"argv": [sys.executable, "-V"], "cwd": "..\\..\\.."}
    operation = registry.build_operation(AgentId.FORGE, "run_process", args)
    await approve(operation)
    with pytest.raises(OperationBlocked) as raised:
        await registry.invoke(AgentId.FORGE, "run_process", args)
    assert "working directory" in raised.value.detail


async def test_adapter_refuses_without_authorization(
    permissions, database, recorder, registry, events_of
) -> None:
    adapter = SafeSubprocess(permissions, database.sessions, recorder)
    operation = registry.build_operation(AgentId.FORGE, "run_process", {"argv": [sys.executable]})
    with pytest.raises(OperationBlocked) as raised:
        await adapter.execute(operation, None)
    assert raised.value.reason is BlockReason.INVALID_AUTHORIZATION
    blocked = await events_of("security.operation_blocked")
    assert "no authorization" in blocked[-1].payload["reason"]


async def test_adapter_refuses_grant_for_a_different_operation(
    permissions, database, recorder, registry, approve
) -> None:
    adapter = SafeSubprocess(permissions, database.sessions, recorder)
    harmless = registry.build_operation(
        AgentId.FORGE, "run_process", {"argv": [sys.executable, "-V"]}
    )
    await approve(harmless)
    grant = await permissions.authorize(harmless)
    harmful = registry.build_operation(
        AgentId.FORGE, "run_process", {"argv": [sys.executable, "-c", "import os"]}
    )
    with pytest.raises(OperationBlocked) as raised:
        await adapter.execute(harmful, grant)
    assert raised.value.reason is BlockReason.INVALID_AUTHORIZATION


async def test_timeout_kills_the_whole_process_tree(registry, approve, roots, database) -> None:
    result = await _approved_run(
        registry,
        approve,
        {"argv": [sys.executable, "-c", CHILD_TREE_SCRIPT], "timeout_seconds": 5},
    )
    assert result.timed_out
    assert not result.succeeded
    pid_file = roots["workspace"] / "child.pid"
    assert pid_file.exists(), "the sleeper did not start its child before the timeout"
    assert _gone(int(pid_file.read_text()))
    async with database.sessions() as session:
        run = await session.get(ExecutionRunRecord, result.run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.resource_usage["timed_out"] is True


async def test_child_environment_is_scrubbed(registry, approve, monkeypatch) -> None:
    monkeypatch.setenv("AETHER_FAKE_API_KEY", "fake-value-for-test")
    monkeypatch.setenv("SERVICE_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("AETHER_HARMLESS_SETTING", "visible")
    script = (
        "import os; print(sorted(name for name in os.environ "
        "if name.startswith(('AETHER_', 'SERVICE_'))))"
    )
    result = await _approved_run(registry, approve, {"argv": [sys.executable, "-c", script]})
    assert result.succeeded
    assert "AETHER_HARMLESS_SETTING" in result.stdout.content
    assert "AETHER_FAKE_API_KEY" not in result.stdout.content
    assert "SERVICE_TOKEN" not in result.stdout.content


def test_scrub_environment_drops_secret_like_names() -> None:
    scrubbed = scrub_environment(
        {
            "PATH": "x",
            "OPENAI_API_KEY": "x",
            "github_token": "x",
            "DB_PASSWORD": "x",
            "DATABASE_URL": "x",
            "AWS_SECRET_ACCESS_KEY": "x",
            "GOOGLE_APPLICATION_CREDENTIALS": "x",
        }
    )
    assert scrubbed == {"PATH": "x"}


async def test_output_is_capped(registry, approve, policy) -> None:
    script = f"import sys; sys.stdout.write('a' * {policy.max_output_bytes * 3})"
    result = await _approved_run(registry, approve, {"argv": [sys.executable, "-c", script]})
    assert result.stdout_truncated
    assert result.stdout_path.stat().st_size == policy.max_output_bytes


async def test_tool_input_is_validated_before_authorization(registry, events_of) -> None:
    with pytest.raises(ToolInputError):
        await registry.invoke(AgentId.FORGE, "run_process", {"argv": []})
    with pytest.raises(ToolInputError):
        await registry.invoke(
            AgentId.FORGE, "run_process", {"argv": [sys.executable], "shell": True}
        )
    assert await events_of("security.operation_authorized") == []


def test_subprocess_operation_shape(registry, policy) -> None:
    operation = registry.build_operation(
        AgentId.FORGE, "run_process", {"argv": [sys.executable, "-V"], "timeout_seconds": 9999}
    )
    assert operation.kind is OperationKind.SUBPROCESS
    assert operation.target == sys.executable
    assert operation.arguments["timeout_seconds"] == policy.subprocess_timeout_seconds
    assert Path(operation.cwd or "") == policy.workspace_dir
