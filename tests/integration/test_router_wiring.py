"""Model router wiring: one router/guard per process and a bound completion facade per agent."""

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlite_schema import copy_schema, copy_schema_to

from aether import main as cli
from aether.agents import AGENT_DEFINITIONS
from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.placeholder import build_placeholder_agents
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.config import AetherSettings
from aether.models import (
    BudgetGuard,
    CompletionRequest,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    ProviderSettings,
)
from aether.models.facade import AgentModels, ModelCallContext, ModelUnavailable
from aether.runtime.bootstrap import Runtime, build_runtime
from aether.security import PolicyMutationError
from aether.storage.models import EventRecord, ProviderUsageRecord, TaskRecord

SECRET = "gsk-should-never-appear"
ALL_AGENTS = [definition.agent_id for definition in AGENT_DEFINITIONS]


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


class ClosableMock(MockProvider):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class FailingStartAgent(BaseAgent):
    agent_id = AgentId.FORGE
    accepted_message_types = frozenset({MessageType.TASK_ASSIGN})

    async def start(self) -> None:
        raise RuntimeError("agent refused to start")

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        return TaskResult(status=TaskResultStatus.COMPLETED)


@pytest.fixture
def settings(tmp_path: Path) -> AetherSettings:
    return AetherSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'router.db').as_posix()}",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        artifact_dir=tmp_path / "artifacts",
        workspace_dir=tmp_path / "workspaces",
        max_concurrent_llm_requests=3,
    )


def provider_settings(**values: Any) -> ProviderSettings:
    values.setdefault("mock", False)
    return ProviderSettings(_env_file=None, max_retries=0, **values)


async def _open(
    settings: AetherSettings,
    providers: list[MockProvider] | None = None,
    agents: list[BaseAgent] | None = None,
) -> Runtime:
    runtime = build_runtime(
        settings,
        agents,
        provider_settings=provider_settings(mock=True),
        model_providers=providers,
    )
    copy_schema_to(settings.database_url.get_secret_value())
    return runtime


@pytest.fixture
async def mock_provider() -> ClosableMock:
    return ClosableMock()


@pytest.fixture
async def runtime(settings: AetherSettings, mock_provider: ClosableMock) -> AsyncIterator[Runtime]:
    runtime = await _open(settings, [mock_provider])
    yield runtime
    await runtime.close()


def _request(**values: Any) -> CompletionRequest:
    return CompletionRequest(
        messages=[{"role": "user", "content": "hello"}],
        max_tokens=8,
        tier=ModelTier.LIGHT,
        **values,
    )


def _models(runtime: Runtime, agent_id: AgentId) -> AgentModels:
    models = runtime.engine.context_for(agent_id).models
    assert models is not None
    return models


async def test_all_seven_agent_contexts_get_a_bound_facade(runtime: Runtime) -> None:
    assert len(ALL_AGENTS) == 7
    facades = []
    for agent_id in ALL_AGENTS:
        ctx = runtime.engine.context_for(agent_id)
        assert isinstance(ctx.models, AgentModels) and ctx.models.agent_id is agent_id
        facades.append(ctx.models)
    assert len({id(facade) for facade in facades}) == 7
    assert runtime.models is not None
    assert runtime.models.router.semaphore._value == 3


async def test_facade_stamps_agent_and_task_and_cannot_impersonate(
    runtime: Runtime, mock_provider: ClosableMock
) -> None:
    task_id, objective_id = uuid4(), uuid4()
    request = _request(agent=AgentId.WARDEN, task_id=uuid4(), objective_id=uuid4())

    response = await _models(runtime, AgentId.SCOUT).complete(
        request, ModelCallContext(task_id=task_id, objective_id=objective_id)
    )

    assert response.provider == "mock"
    (seen,) = mock_provider.requests
    assert (seen.agent, seen.task_id, seen.objective_id) == (AgentId.SCOUT, task_id, objective_id)
    assert request.agent is AgentId.WARDEN  # the caller's request is never mutated
    async with runtime.database.sessions() as session:
        rows = list(await session.scalars(select(ProviderUsageRecord)))
        events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "provider.request_completed")
            )
        )
    assert [(row.agent, row.task_id) for row in rows] == [("SCOUT", task_id)]
    assert [event.actor for event in events] == ["SCOUT"]


async def test_facade_clears_task_ids_without_a_call_context(
    runtime: Runtime, mock_provider: ClosableMock
) -> None:
    await _models(runtime, AgentId.LEDGER).complete(_request(task_id=uuid4()))

    (seen,) = mock_provider.requests
    assert seen.agent is AgentId.LEDGER and seen.task_id is None


async def test_facade_cannot_be_rebound_or_asked_for_another_identity(runtime: Runtime) -> None:
    models = _models(runtime, AgentId.SCOUT)
    with pytest.raises(AttributeError):
        models.agent_id = AgentId.WARDEN  # type: ignore[misc]
    with pytest.raises(AttributeError):
        models._complete = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        await models.complete(_request(), agent=AgentId.WARDEN)  # type: ignore[call-arg]
    assert repr(models) == "AgentModels(agent_id='SCOUT')"


async def test_tier_none_is_rejected_without_reaching_a_provider(
    runtime: Runtime, mock_provider: ClosableMock
) -> None:
    with pytest.raises(ValueError, match="deterministic"):
        await _models(runtime, AgentId.SCOUT).complete(
            CompletionRequest(messages=[{"role": "user", "content": "x"}], tier=ModelTier.NONE)
        )
    assert mock_provider.requests == []


async def test_no_eligible_model_surfaces_as_a_facade_error(settings: AetherSettings) -> None:
    offline = ClosableMock(available=False)
    runtime = await _open(settings, [offline])
    try:
        with pytest.raises(ModelUnavailable) as caught:
            await _models(runtime, AgentId.SCOUT).complete(_request())
        assert caught.value.reasons == ["mock: unavailable"]
    finally:
        await runtime.close()


def _reachable(root: object) -> list[object]:
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


async def test_contexts_expose_no_router_guard_or_provider(runtime: Runtime) -> None:
    privileged = (ModelRouter, BudgetGuard, MockProvider)
    for agent_id in ALL_AGENTS:
        ctx = runtime.engine.context_for(agent_id)
        public = {name for name in dir(ctx.models) if not name.startswith("_")}
        assert public == {"agent_id", "complete", "report_unusable"}
        assert not [item for item in _reachable(ctx) if isinstance(item, privileged)], agent_id


async def test_router_and_guard_are_shared_by_all_agents(runtime: Runtime) -> None:
    assert runtime.models is not None
    assert runtime.models.router.budgets is runtime.models.budgets
    assert runtime.engine.models is runtime.models


async def test_router_closes_at_shutdown(settings: AetherSettings, isolated_logging: None) -> None:
    provider = ClosableMock()
    runtime = await _open(settings, [provider])
    try:
        await runtime.run(once=True)
        assert provider.closed == 1
    finally:
        await runtime.close()
    assert provider.closed == 1  # close is idempotent


async def test_router_closes_when_startup_fails(
    settings: AetherSettings, isolated_logging: None
) -> None:
    provider = ClosableMock()
    others = [a for a in build_placeholder_agents() if a.agent_id is not AgentId.FORGE]
    runtime = await _open(settings, [provider], [FailingStartAgent(), *others])
    try:
        with pytest.raises(RuntimeError, match="refused to start"):
            await runtime.run(once=True)
        assert provider.closed == 1
    finally:
        await runtime.close()


async def test_close_without_running_closes_the_router(settings: AetherSettings) -> None:
    provider = ClosableMock()
    runtime = await _open(settings, [provider])
    await runtime.close()
    assert provider.closed == 1


async def test_models_initialized_event_has_no_secrets(
    settings: AetherSettings, isolated_logging: None
) -> None:
    runtime = build_runtime(
        settings,
        provider_settings=provider_settings(GROQ_API_KEY=SECRET),
    )
    copy_schema_to(settings.database_url.get_secret_value())
    try:
        await runtime.run(once=True)
        async with runtime.database.sessions() as session:
            events = list(
                await session.scalars(
                    select(EventRecord).where(
                        EventRecord.event_type == "runtime.models_initialized"
                    )
                )
            )
    finally:
        await runtime.close()

    assert len(events) == 1
    payload = events[0].payload
    by_name = {item["name"]: item for item in payload["providers"]}
    assert by_name["groq"] == {
        "name": "groq",
        "configured": True,
        "available": True,
        "model_count": 3,
    }
    assert by_name["ollama"]["available"] is False
    assert by_name["azure_openai"]["configured"] is False
    assert payload["mock"] is False and payload["max_concurrent_llm_requests"] == 3
    assert payload["model_count"] == 3
    assert SECRET not in str(payload)


async def test_provider_hosts_are_in_the_frozen_policy(settings: AetherSettings) -> None:
    runtime = build_runtime(
        settings,
        provider_settings=provider_settings(
            GROQ_API_KEY=SECRET,
            AZURE_OPENAI_ENDPOINT="https://astra.example.com",
            AZURE_OPEN_AI_KEY=SECRET,
            OPENAI_API_VERSION="2026-01-01",
            ANTHROPIC_ENDPOINT="https://opus.example.com/anthropic",
            ANTHROPIC_API_KEY=SECRET,
            API_VERSION="2026-01-01",
        ),
    )
    try:
        policy = runtime.security.policy
        for host in ("api.groq.com", "astra.example.com", "opus.example.com", "localhost"):
            assert policy.is_host_allowed(host), host
        assert not policy.is_host_allowed("evil.example.com")
        with pytest.raises(PolicyMutationError):
            policy.allowed_hosts = frozenset({"evil.example.com"})  # type: ignore[misc]
        assert isinstance(policy.allowed_hosts, frozenset)
    finally:
        await runtime.close()


async def test_mock_mode_adds_no_hosts_and_uses_only_the_mock_provider(
    settings: AetherSettings,
) -> None:
    runtime = build_runtime(
        settings, provider_settings=provider_settings(mock=True, GROQ_API_KEY=SECRET)
    )
    try:
        assert runtime.security.policy.allowed_hosts == frozenset()
        assert runtime.models is not None
        assert [provider.name for provider in runtime.models.providers] == ["mock"]
    finally:
        await runtime.close()


def test_mock_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")
    assert ProviderSettings(_env_file=None).mock is True


def _cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "GROQ_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPEN_AI_KEY",
        "OPENAI_API_VERSION",
        "ANTHROPIC_ENDPOINT",
        "ANTHROPIC_API_KEY",
        "API_VERSION",
        "OLLAMA_BASE_URL",
        "AETHER_PROVIDER_OLLAMA_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "AETHER_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'cli.db').as_posix()}"
    )
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AETHER_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    copy_schema(tmp_path / "cli.db")


def test_models_status_reports_usage_and_no_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "0")
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://private-astra.example.com")
    monkeypatch.setenv("AZURE_OPEN_AI_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_VERSION", "2026-01-01")

    cli.main(["models", "status"])
    out = capsys.readouterr()

    assert "groq: configured=yes available=yes models=openai/gpt-oss-20b" in out.out
    assert "azure_openai: configured=yes available=yes models=gpt-6-astra" in out.out
    assert "azure_anthropic: configured=no available=no models=-" in out.out
    assert "ollama: configured=no available=no models=-" in out.out
    # Groq quotas are per model; the provider line keeps only its cost ceiling.
    assert "today: requests 0/unlimited, tokens 0/unlimited, cost $0.000000/$10.00" in out.out
    # Groq keeps its budget line; Azure caps are a silent safety net with no usage display.
    assert out.out.count("  today: requests ") == 2
    assert "requests 0/1000" not in out.out and "tokens 0/3000000" not in out.out
    assert "cost unknown" not in out.out and "AETHER_PROVIDER_PRICES" not in out.out
    assert (
        "ORCHESTR: 1. azure_openai/gpt-6-astra, 2. azure_anthropic/claude-opus-4-8 (unavailable),"
        " 3. groq/openai/gpt-oss-120b" in out.out
    )
    assert "LEDGER: 1. azure_openai/gpt-6-astra" in out.out
    assert (
        "FORGE: 1. azure_anthropic/claude-opus-4-8 (unavailable), 2. azure_openai/gpt-6-astra"
        in (out.out)
    )
    assert out.out.count("today: no usage") == 7
    for forbidden in (SECRET, "private-astra", "https://", "2026-01-01"):
        assert forbidden not in out.out + out.err


def test_models_status_reports_todays_usage_per_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from sqlite_schema import sqlite_url

    from aether.storage.database import Database
    from aether.storage.repositories.provider_usage import ProviderUsageRepository

    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "0")
    monkeypatch.setenv(
        "AETHER_MODEL_ASSIGNMENT", '{"scout": ["groq/openai/gpt-oss-120b"], "default": []}'
    )

    async def record() -> None:
        database = Database(sqlite_url(tmp_path / "cli.db"))
        try:
            usage = ProviderUsageRepository(database.sessions)
            for _ in range(2):
                await usage.record(
                    provider="azure_openai",
                    model="gpt-6-astra",
                    agent="ORCHESTR",
                    input_tokens=100,
                    output_tokens=20,
                )
            await usage.record(
                provider="groq", model="openai/gpt-oss-20b", agent="LEDGER", input_tokens=5
            )
        finally:
            await database.close()

    asyncio.run(record())
    cli.main(["models", "status"])
    out = capsys.readouterr().out

    lines = out.splitlines()
    orchestr = lines.index(next(line for line in lines if line.startswith("ORCHESTR: ")))
    assert lines[orchestr + 1] == "  today: azure_openai/gpt-6-astra requests 2"
    ledger = lines.index(next(line for line in lines if line.startswith("LEDGER: ")))
    assert lines[ledger + 1] == "  today: groq/openai/gpt-oss-20b requests 1"
    assert not any(line.startswith("azure_openai: ") and "tokens" in line for line in lines)
    azure = lines.index(next(line for line in lines if line.startswith("azure_openai: ")))
    assert not lines[azure + 1].startswith("  today:")
    assert "SCOUT: 1. groq/openai/gpt-oss-120b (unavailable)" in out
    assert "default: no assignment (final fallback only)" in out


def test_models_test_runs_one_completion_in_mock_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")

    cli.main(["models", "test", "--tier", "light"])
    out = capsys.readouterr().out.splitlines()

    assert out[0] == "Provider: mock"
    assert out[1] == "Model: mock-model"
    assert out[2].startswith("Tokens: ") and out[3].startswith("Cost: $")
    cli.main(["models", "status"])
    assert "requests 1/unlimited" in capsys.readouterr().out


def test_models_test_fails_cleanly_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cli_env(tmp_path, monkeypatch)
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "0")

    with pytest.raises(SystemExit) as exited:
        cli.main(["models", "test", "--tier", "premium"])

    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert "No model available" in captured.err and captured.out == ""


def test_models_test_rejects_tier_none() -> None:
    with pytest.raises(SystemExit):
        cli.main(["models", "test", "--tier", "none"])


async def test_unused_provider_model_listing_is_static(settings: AetherSettings) -> None:
    custom = ClosableMock(models=[ModelInfo(name="tiny", tiers=frozenset({ModelTier.LIGHT}))])
    runtime = await _open(settings, [custom])
    try:
        assert runtime.models is not None
        (status,) = await runtime.models.describe()
        assert (status.name, status.models, status.available) == ("mock", ("tiny",), True)
    finally:
        await runtime.close()
