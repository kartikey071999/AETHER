"""Scenario 5: model providers fail transiently.

Transient errors are retried with bounded, capped backoff, and every attempt is metered. When a
provider stays down the router falls back to the next one. When every provider is down the
agent sees ``ModelUnavailable``, the task is handed back for a later attempt instead of being
lost, and it completes once a provider recovers.
"""

from uuid import UUID

import pytest
from reliability_support import (
    build_engine,
    event_counts,
    get_task,
    make_task,
    task_state,
    wait_until,
)
from sqlalchemy import select

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage
from aether.models import (
    BudgetGuard,
    CompletionRequest,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    ProviderError,
    ProviderSettings,
)
from aether.models.facade import ModelCallContext, ModelUnavailable
from aether.models.governor import ModelGovernor
from aether.runtime.models import ModelServices
from aether.storage.models import EventRecord, ProviderUsageRecord, TaskRecord, TaskStatus
from aether.storage.repositories.provider_usage import ProviderUsageRepository


def _settings() -> ProviderSettings:
    return ProviderSettings(
        _env_file=None,
        max_retries=2,
        retry_backoff_seconds=0.5,
        retry_max_delay_seconds=0.75,
        prices={
            f"{name}/{name}-model": {"input_per_mtok": 0, "output_per_mtok": 0}
            for name in ("groq", "ollama")
        },
    )


def _provider(name: str, responses: list | None = None) -> MockProvider:
    return MockProvider(
        responses,
        name=name,
        models=[ModelInfo(name=f"{name}-model", tiers=frozenset({ModelTier.STANDARD}))],
    )


def _transient(name: str, count: int) -> list[ProviderError]:
    return [ProviderError(name, transient=True) for _ in range(count)]


def _request() -> CompletionRequest:
    return CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}], tier=ModelTier.STANDARD
    )


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def _usage(sessions) -> list[tuple[str, str]]:
    async with sessions() as session:
        rows = await session.scalars(
            select(ProviderUsageRecord).order_by(ProviderUsageRecord.occurred_at)
        )
        return [(row.provider, row.status) for row in rows]


def _services(sessions, providers: list[MockProvider], sleeper: _Sleeps) -> ModelServices:
    settings = _settings()
    usage = ProviderUsageRepository(sessions)
    budgets = BudgetGuard(usage, settings)
    return ModelServices(
        settings=settings,
        providers=tuple(providers),
        budgets=budgets,
        router=ModelRouter(providers, budgets, sleeper=sleeper),
        usage=usage,
        max_concurrent_llm_requests=2,
        governor=ModelGovernor(settings, sessions=sessions),
    )


async def test_transient_failures_are_retried_with_capped_backoff_until_success(
    sessions,
) -> None:
    sleeps = _Sleeps()
    groq = _provider("groq", [*_transient("groq", 2), "recovered"])
    services = _services(sessions, [groq], sleeps)

    response = await services.router.complete(_request())

    assert (response.provider, response.text) == ("groq", "recovered")
    assert sleeps.delays == [0.5, 0.75]
    assert len(groq.requests) == 3
    assert await _usage(sessions) == [
        ("groq", "failed"),
        ("groq", "failed"),
        ("groq", "succeeded"),
    ]
    async with sessions() as session:
        events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "provider.request_completed")
            )
        )
    assert sorted(event.payload["attempt"] for event in events) == [1, 2, 3]


async def test_a_provider_that_stays_down_falls_back_to_the_next_one(sessions) -> None:
    sleeps = _Sleeps()
    local = _provider("ollama", _transient("ollama", 3))
    groq = _provider("groq")
    services = _services(sessions, [groq, local], sleeps)

    response = await services.router.complete(_request())

    assert response.provider == "groq"
    assert len(local.requests) == 3  # 1 + max_retries, then the router moves on
    assert len(groq.requests) == 1
    assert sleeps.delays == [0.5, 0.75]
    usage = await _usage(sessions)
    assert usage.count(("ollama", "failed")) == 3 and usage.count(("groq", "succeeded")) == 1


async def test_non_transient_errors_are_not_retried(sessions) -> None:
    sleeps = _Sleeps()
    local = _provider("ollama", [ProviderError("ollama", code="bad_request")])
    groq = _provider("groq")
    services = _services(sessions, [groq, local], sleeps)

    assert (await services.router.complete(_request())).provider == "groq"
    assert len(local.requests) == 1 and sleeps.delays == []


async def test_all_providers_down_raises_model_unavailable_to_the_agent(sessions) -> None:
    sleeps = _Sleeps()
    services = _services(
        sessions,
        [_provider("ollama", _transient("ollama", 3)), _provider("groq", _transient("groq", 3))],
        sleeps,
    )
    ctx = services.context_for(
        AgentId.FORGE, AgentContext(sessions=sessions, bus=MessageBus(sessions))
    )

    with pytest.raises(ModelUnavailable) as raised:
        await ctx.models.complete(_request())

    assert len(raised.value.reasons) >= 2
    assert len(await _usage(sessions)) == 6


class ModelBackedAgent(BaseAgent):
    """FORGE stand-in that needs one completion per task and degrades when none is possible."""

    agent_id = AgentId.FORGE

    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[str] = []

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        assert ctx.models is not None
        try:
            response = await ctx.models.complete(
                _request(), ModelCallContext(task_id=task.id, objective_id=task.objective_id)
            )
        except ModelUnavailable as error:
            self.outcomes.append("unavailable")
            return TaskResult(
                status=TaskResultStatus.FAILED,
                summary="no model available",
                error=str(error),
                retryable=True,
            )
        self.outcomes.append(response.provider)
        return TaskResult(status=TaskResultStatus.COMPLETED, summary=response.text)


async def test_engine_retries_a_task_after_a_full_provider_outage(
    sessions, objective_id: UUID
) -> None:
    sleeps = _Sleeps()
    # Attempt 1: every call fails (3 tries each); attempt 2: the provider has recovered.
    groq = _provider("groq", [*_transient("groq", 3), "back online"])
    agent = ModelBackedAgent()
    engine = build_engine(sessions, [agent], models=_services(sessions, [groq], sleeps))
    task_id = await make_task(sessions, objective_id, max_attempts=3)

    await engine.start()
    try:
        await wait_until(lambda: _is(sessions, task_id, TaskStatus.COMPLETED), timeout=10.0)
    finally:
        await engine.stop()

    assert agent.outcomes == ["unavailable", "groq"]
    task = await get_task(sessions, task_id)
    assert task.attempt == 2
    counts = await event_counts(sessions, task_id)
    assert (counts["task.failed"], counts["task.completed"]) == (1, 1)
    async with sessions() as session:
        metered = list(
            await session.scalars(
                select(ProviderUsageRecord.status).where(ProviderUsageRecord.task_id == task_id)
            )
        )
    assert sorted(metered) == ["failed", "failed", "failed", "succeeded"]


async def _is(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state
