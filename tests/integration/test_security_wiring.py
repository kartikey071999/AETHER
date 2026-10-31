"""Security wiring: one policy/permission/approval/tool set injected into every agent context."""

import logging
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from aether import main as cli
from aether.agents import AGENT_DEFINITIONS
from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.placeholder import build_placeholder_agents
from aether.agents.security_review import request_security_review
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.config import AetherSettings
from aether.runtime.bootstrap import Runtime, build_runtime
from aether.security import (
    ApprovalService,
    BlockReason,
    Operation,
    OperationBlocked,
    OperationKind,
    PermissionService,
    SecuritySettings,
)
from aether.security.readonly import PermissionView
from aether.storage.models import Base, EventRecord, MessageRecord, TaskRecord, TaskStatus
from aether.storage.repositories import ObjectiveRepository
from aether.tools import AgentTools, Tool, ToolRegistry
from aether.tools.adapters import SafeFiles, SafeHttp, SafeSubprocess

SECRET = "pw-should-never-appear"
ALL_AGENTS = [definition.agent_id for definition in AGENT_DEFINITIONS]
PRINT_ARGV = [sys.executable, "-c", "print('wired')"]


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    logger = logging.getLogger("aether")
    handlers = logger.handlers[:]
    level, propagate = logger.level, logger.propagate
    logger.handlers = []
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


class ProcessAgent(BaseAgent):
    """Stands in for FORGE: runs one allowlist-checked process through `invoke_tool`."""

    agent_id = AgentId.FORGE
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self.argv = argv

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        outcome = await self.invoke_tool("run_process", {"argv": self.argv}, ctx, task=task)
        if not outcome.ok:
            assert outcome.result is not None
            return outcome.result
        return TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary="ran process",
            outputs={"stdout": outcome.value.stdout.content, "run_id": str(outcome.value.run_id)},
        )


def _agents(argv: list[str]) -> list[BaseAgent]:
    others = [agent for agent in build_placeholder_agents() if agent.agent_id is not AgentId.FORGE]
    return [ProcessAgent(argv), *others]


def _security_settings() -> SecuritySettings:
    return SecuritySettings(
        _env_file=None,
        allowed_executables=[sys.executable],
        allowed_hosts=["api.example.com"],
    )


@pytest.fixture
def settings(tmp_path: Path) -> AetherSettings:
    return AetherSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'wiring.db').as_posix()}",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        artifact_dir=tmp_path / "artifacts",
        workspace_dir=tmp_path / "workspaces",
    )


async def _open(settings: AetherSettings, argv: list[str]) -> Runtime:
    runtime = build_runtime(settings, _agents(argv), security_settings=_security_settings())
    async with runtime.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return runtime


@pytest.fixture
async def runtime(settings: AetherSettings) -> AsyncIterator[Runtime]:
    runtime = await _open(settings, PRINT_ARGV)
    yield runtime
    await runtime.close()


async def _run_task(runtime: Runtime) -> tuple[UUID, TaskRecord]:
    sessions = runtime.database.sessions
    objective_id = await ObjectiveRepository(sessions).create(title="Security wiring")
    async with sessions() as session, session.begin():
        task = TaskRecord(
            objective_id=objective_id,
            title="run a process",
            state=TaskStatus.READY.value,
            assigned_agent="FORGE",
        )
        session.add(task)
        await session.flush()
        task_id = task.id
    assert await runtime.engine.pool.submit(task_id, runtime.engine.agent(AgentId.FORGE))
    await runtime.engine.pool.join()
    async with sessions() as session:
        stored = await session.get(TaskRecord, task_id, populate_existing=True)
    assert stored is not None
    return task_id, stored


async def _events(runtime: Runtime, event_type: str) -> list[EventRecord]:
    async with runtime.database.sessions() as session:
        statement = select(EventRecord).where(EventRecord.event_type == event_type)
        return list(await session.scalars(statement))


def _forge_operation(runtime: Runtime, argv: list[str]) -> Operation:
    return runtime.security.tools.build_operation(AgentId.FORGE, "run_process", {"argv": argv})


async def test_container_injects_the_same_services_into_all_seven_agents(
    runtime: Runtime,
) -> None:
    assert len(ALL_AGENTS) == 7
    security = runtime.security
    for agent_id in ALL_AGENTS:
        ctx = runtime.engine.context_for(agent_id)
        assert isinstance(ctx.permission_service, PermissionView)
        assert isinstance(ctx.tools, AgentTools) and ctx.tools.agent_id is agent_id
        assert {tool.name for tool in ctx.tools.list_tools()} == set(security.tools.tools)
        assert ctx.bus is runtime.engine.bus
    assert set(security.tools.tools) == {
        "run_process",
        "http_request",
        "read_file",
        "write_file",
        "deliver_outbox_message",
    }


def _reachable(root: object) -> list[object]:
    """Every object reachable through instance attributes, slots and containers of aether code."""
    seen: dict[int, object] = {}
    pending = [root]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen[id(item)] = item
        if isinstance(item, dict):
            pending += list(item.keys()) + list(item.values())
        elif isinstance(item, list | tuple | set | frozenset):
            pending += list(item)
        elif type(item).__module__.startswith("aether") and not isinstance(item, type):
            names = [n for c in type(item).__mro__ for n in getattr(c, "__slots__", ())]
            names += list(getattr(item, "__dict__", {}))
            pending += [getattr(item, name, None) for name in names if not name.startswith("__")]
    return list(seen.values())


async def test_agent_contexts_expose_no_registry_adapter_or_authorize(runtime: Runtime) -> None:
    privileged = (ToolRegistry, Tool, PermissionService, SafeSubprocess, SafeHttp, SafeFiles)
    for agent_id in ALL_AGENTS:
        ctx = runtime.engine.context_for(agent_id)
        assert not hasattr(ctx.permission_service, "authorize")
        assert not hasattr(ctx.permission_service, "redeem")
        assert not hasattr(ctx.tools, "registry")
        reachable = _reachable(ctx)
        assert not [item for item in reachable if isinstance(item, privileged)], agent_id
        approvals = [item for item in reachable if isinstance(item, ApprovalService)]
        assert bool(approvals) is (agent_id is AgentId.WARDEN)
        with pytest.raises(AttributeError):
            ctx.tools.agent_id = AgentId.WARDEN  # type: ignore[union-attr,misc]


async def test_agent_cannot_invoke_a_tool_as_another_agent(runtime: Runtime) -> None:
    approvals = runtime.engine.context_for(AgentId.WARDEN).services["approvals"]
    await approvals.record_decision(
        AgentId.WARDEN,
        _forge_operation(runtime, PRINT_ARGV),
        "approved",
        rationale="FORGE may run this exact argv",
        evidence=[{"check": "integration-test"}],
    )
    scout_tools = runtime.engine.context_for(AgentId.SCOUT).tools
    assert scout_tools is not None

    with pytest.raises(TypeError):
        await scout_tools.invoke("run_process", {"argv": PRINT_ARGV}, agent_id=AgentId.FORGE)  # type: ignore[call-arg]
    with pytest.raises(OperationBlocked) as blocked:
        await scout_tools.invoke("run_process", {"argv": PRINT_ARGV})

    assert blocked.value.reason is BlockReason.AGENT_NOT_PERMITTED
    assert scout_tools.build_operation("run_process", {"argv": PRINT_ARGV}).requested_by is (
        AgentId.SCOUT
    )
    assert await _events(runtime, "execution.started") == []
    events = await _events(runtime, "security.operation_blocked")
    assert [event.actor for event in events] == ["SCOUT"]


async def test_only_warden_context_holds_the_approval_service(runtime: Runtime) -> None:
    for agent_id in ALL_AGENTS:
        services = runtime.engine.context_for(agent_id).services
        if agent_id is AgentId.WARDEN:
            assert services["approvals"] is runtime.security.approvals
        else:
            assert "approvals" not in services


async def test_allowlisted_tool_call_succeeds_and_is_recorded(runtime: Runtime) -> None:
    approvals = runtime.engine.context_for(AgentId.WARDEN).services["approvals"]
    await approvals.record_decision(
        AgentId.WARDEN,
        _forge_operation(runtime, PRINT_ARGV),
        "approved",
        rationale="allowlisted interpreter, fixed argv",
        evidence=[{"check": "integration-test"}],
    )

    _, task = await _run_task(runtime)

    assert task.state == TaskStatus.COMPLETED.value
    assert len(await _events(runtime, "execution.started")) == 1
    finished = await _events(runtime, "execution.finished")
    assert [event.payload["success"] for event in finished] == [True]
    assert len(await _events(runtime, "security.operation_authorized")) == 1


async def test_non_allowlisted_call_returns_a_structured_blocked_result(
    settings: AetherSettings,
) -> None:
    unlisted = str(Path(sys.executable).with_name("not-allowlisted.exe"))
    runtime = await _open(settings, [unlisted, "-V"])
    try:
        _, task = await _run_task(runtime)

        assert task.state == TaskStatus.BLOCKED.value
        blocked = await _events(runtime, "security.operation_blocked")
        assert len(blocked) == 1
        assert "policy_violation" in blocked[0].payload["reason"]
        assert await _events(runtime, "execution.started") == []
        result = (await _events(runtime, "task.blocked"))[0].payload["result"]
        assert result["status"] == "blocked"
        assert result["outputs"]["block_reason"] == "policy_violation"
        assert len(result["outputs"]["input_hash"]) == 64
    finally:
        await runtime.close()


async def test_approval_requiring_operation_waits_and_notifies_warden(
    runtime: Runtime,
) -> None:
    operation = _forge_operation(runtime, PRINT_ARGV)

    task_id, task = await _run_task(runtime)

    assert task.state == TaskStatus.WAITING_FOR_APPROVAL.value
    assert await _events(runtime, "execution.started") == []
    async with runtime.database.sessions() as session:
        messages = list(await session.scalars(select(MessageRecord)))
    assert len(messages) == 1
    message = messages[0]
    assert message.message_type == MessageType.SECURITY_EVALUATE.value == "security.evaluate"
    assert (message.source_agent, message.target_agent) == ("FORGE", "WARDEN")
    assert message.task_id == task_id
    assert message.payload["input_hash"] == operation.input_hash
    reviewed = Operation.model_validate(message.payload["operation"])
    assert reviewed.input_hash == operation.input_hash
    assert len(await _events(runtime, "security.operation_blocked")) == 1


async def test_request_security_review_refuses_wardens_own_operation(runtime: Runtime) -> None:
    operation = Operation(
        kind=OperationKind.FILE_READ, target="notes.txt", requested_by=AgentId.WARDEN
    )

    result = await request_security_review(operation, runtime.engine.context_for(AgentId.WARDEN))

    assert result.status is TaskResultStatus.BLOCKED
    assert await runtime.engine.bus.pending_count(AgentId.WARDEN) == 0


async def test_invoke_tool_without_registry_fails_without_side_effects(
    runtime: Runtime,
) -> None:
    ctx = runtime.engine.context_for(AgentId.FORGE)
    bare = AgentContext(sessions=ctx.sessions, bus=ctx.bus)

    outcome = await ProcessAgent(PRINT_ARGV).invoke_tool("run_process", {}, bare)

    assert not outcome.ok and outcome.result is not None
    assert outcome.result.status is TaskResultStatus.FAILED
    assert outcome.result.retryable is False


async def test_start_emits_security_initialized_without_secrets(
    settings: AetherSettings, isolated_logging: None
) -> None:
    runtime = await _open(settings, [sys.executable, "-V"])
    try:
        await runtime.run(once=True)
        events = await _events(runtime, "runtime.security_initialized")
    finally:
        await runtime.close()

    assert len(events) == 1
    payload: dict[str, Any] = events[0].payload
    assert payload["policy_fingerprint"] == runtime.security.fingerprint
    assert payload["tool_count"] == 5
    assert payload["allowlist_sizes"]["hosts"] == 1
    assert payload["allowlist_sizes"]["executables"] == 1
    assert sys.executable not in str(payload)


def test_security_show_prints_fingerprint_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", f"postgresql://aether:{SECRET}@127.0.0.1:1/aether")
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AETHER_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AETHER_SECURITY_ALLOWED_HOSTS", '["api.example.com"]')

    cli.main(["security", "show"])
    first = capsys.readouterr()
    cli.main(["security", "show"])
    second = capsys.readouterr()

    fingerprint = first.out.splitlines()[0].removeprefix("Policy fingerprint: ")
    assert len(fingerprint) == 64 and int(fingerprint, 16) >= 0
    assert first.out == second.out
    assert "api.example.com" in first.out
    assert "subprocess" in first.out
    assert SECRET not in first.out + first.err
