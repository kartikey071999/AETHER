"""Bounded LLM spend: fallback cap, one length retry, per-objective budgets, circuit breaker."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from model_helpers import records
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.models import (
    BudgetGuard,
    BudgetPolicy,
    CompletionRequest,
    CompletionResponse,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    ProviderError,
    ProviderSettings,
    RouteSpend,
    TruncatedCompletion,
)
from aether.models.facade import AgentBudgetExceeded, AgentModels, ModelCallContext
from aether.models.governor import ModelGovernor
from aether.observability.recorder import EventRecorder
from aether.storage.models import EventRecord, ProviderUsageRecord

STANDARD = frozenset({ModelTier.STANDARD})


def request(**overrides) -> CompletionRequest:
    return CompletionRequest(
        **{"messages": [{"role": "user", "content": "Plan"}], "max_tokens": 100, **overrides}
    )


class TruncatingProvider(MockProvider):
    """Every reply is cut off at the requested output limit (the incident's failure mode)."""

    def __init__(self, models: list[str], *, input_tokens: int = 50) -> None:
        super().__init__(
            name="mock", models=[ModelInfo(name=name, tiers=STANDARD) for name in models]
        )
        self.input_tokens = input_tokens

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        return CompletionResponse(
            text='{"steps": [{"tool": "write_file", "arguments": {"path": "a',
            provider=self.name,
            model=request.model or "",
            input_tokens=self.input_tokens,
            output_tokens=request.max_tokens,
            latency_ms=0,
            finish_reason="length",
        )


def settings_with(**updates) -> ProviderSettings:
    return ProviderSettings(_env_file=None, max_retries=0, **updates)


async def test_router_tries_at_most_two_models_by_default(usage):
    failing = MockProvider(
        [ProviderError("mock"), ProviderError("mock"), ProviderError("mock")],
        name="mock",
        models=[ModelInfo(name=f"m{i}", tiers=STANDARD) for i in range(3)],
    )
    guard = BudgetGuard(usage, settings_with())
    with pytest.raises(NoModelAvailable) as caught:
        await ModelRouter([failing], guard).complete(request())
    assert len(failing.requests) == 2
    assert "fallback limit reached" in caught.value.reasons[-1]


async def test_budget_rejected_models_do_not_count_towards_the_model_cap(usage):
    provider = MockProvider(
        [ProviderError("mock"), "ok"],
        name="mock",
        models=[ModelInfo(name=f"m{i}", tiers=STANDARD) for i in range(3)],
    )
    settings = settings_with(model_budgets={"mock/m0": {"max_tokens_per_request": 10}})
    result = await ModelRouter([provider], BudgetGuard(usage, settings)).complete(request())
    assert result.model == "m2"
    assert [seen.model for seen in provider.requests] == ["m1", "m2"]


async def test_truncated_json_retries_once_on_the_same_model_with_doubled_tokens(usage):
    provider = MockProvider(
        [
            CompletionResponse(
                text='{"steps": [',
                provider="mock",
                model="m0",
                input_tokens=10,
                output_tokens=100,
                latency_ms=0,
                finish_reason="length",
            ),
            CompletionResponse(
                text='{"steps": []}',
                provider="mock",
                model="m0",
                input_tokens=10,
                output_tokens=20,
                latency_ms=0,
                finish_reason="stop",
            ),
        ],
        name="mock",
        models=[ModelInfo(name="m0", tiers=STANDARD), ModelInfo(name="m1", tiers=STANDARD)],
    )
    spend = RouteSpend()
    result = await ModelRouter([provider], BudgetGuard(usage, settings_with())).complete(
        request(response_format="json_object"), spend=spend
    )
    assert result.text == '{"steps": []}'
    assert [(seen.model, seen.max_tokens) for seen in provider.requests] == [
        ("m0", 100),
        ("m0", 200),
    ]
    assert (spend.requests, spend.input_tokens, spend.output_tokens) == (2, 20, 120)


async def test_length_retry_is_capped_and_happens_only_once(usage, database):
    provider = TruncatingProvider(["m0", "m1", "m2"])
    settings = settings_with(length_retry_max_tokens=150)
    with pytest.raises(NoModelAvailable) as caught:
        await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
            request(response_format="json_object")
        )
    # Two models, each with one capped length retry: 4 calls instead of an unbounded loop.
    assert [(seen.model, seen.max_tokens) for seen in provider.requests] == [
        ("m0", 100),
        ("m0", 150),
        ("m1", 100),
        ("m1", 150),
    ]
    assert TruncatedCompletion.__name__ in caught.value.reasons[0]
    rows, _ = await records(database)
    assert sorted(row.output_tokens for row in rows) == [100, 100, 150, 150]


async def test_truncated_plain_text_is_still_returned(usage):
    provider = TruncatingProvider(["m0"])
    result = await ModelRouter([provider], BudgetGuard(usage, settings_with())).complete(request())
    assert result.finish_reason == "length" and len(provider.requests) == 1


async def test_route_spend_refuses_calls_beyond_its_limits(usage):
    provider = TruncatingProvider(["m0", "m1"])
    spend = RouteSpend(max_tokens=request().input_token_estimate() + 150)
    with pytest.raises(NoModelAvailable):
        await ModelRouter([provider], BudgetGuard(usage, settings_with())).complete(
            request(response_format="json_object"), spend=spend
        )
    assert len(provider.requests) == 1  # the doubled retry would not fit the allowance
    assert spend.refusal == "objective token budget"


def governed(
    database,
    usage,
    provider,
    settings: ProviderSettings,
    agent: AgentId = AgentId.FORGE,
    clock=None,
) -> tuple[AgentModels, ModelGovernor]:
    router = ModelRouter([provider], BudgetGuard(usage, settings))
    extra = {"clock": clock} if clock is not None else {}
    governor = ModelGovernor(
        settings, sessions=database.sessions, recorder=EventRecorder(), **extra
    )

    async def complete(req: CompletionRequest) -> CompletionResponse:
        return await governor.complete(req, lambda r, s: router.complete(r, spend=s))

    return AgentModels(agent, complete, report_unusable=governor.report_unusable), governor


async def events_of(database, event_type: str) -> list[EventRecord]:
    async with database.sessions() as session:
        return list(
            await session.scalars(select(EventRecord).where(EventRecord.event_type == event_type))
        )


async def test_per_objective_token_budget_stops_llm_use_and_records_an_event(database, usage):
    provider = TruncatingProvider(["m0", "m1", "m2"])
    settings = settings_with(
        agent_budgets={"FORGE": {"max_tokens_per_objective": 1000}},
        circuit_breaker_failures=0,
    )
    models, governor = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(10):
        with pytest.raises(Exception):  # noqa: B017 - ModelUnavailable or AgentBudgetExceeded
            await models.complete(request(response_format="json_object"), call)
    with pytest.raises(AgentBudgetExceeded):
        await models.complete(request(response_format="json_object"), call)
    requests, tokens, _ = governor.usage(call.objective_id, AgentId.FORGE)
    assert tokens <= 1000
    assert requests == len(provider.requests) <= 6
    [event] = await events_of(database, "provider.budget_exhausted")
    assert event.payload["agent"] == "FORGE" and event.objective_id == call.objective_id
    # Another objective and another agent keep their own allowance.
    other = ModelCallContext(objective_id=uuid4())
    calls = len(provider.requests)
    with pytest.raises(Exception):  # noqa: B017
        await models.complete(request(response_format="json_object"), other)
    assert len(provider.requests) > calls


async def test_concurrent_calls_cannot_spend_the_same_allowance_twice(database, usage):
    class SlowProvider(MockProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            await asyncio.sleep(0.05)
            return await super().complete(request)

    provider = SlowProvider(name="mock", models=[ModelInfo(name="m0", tiers=STANDARD)])
    settings = settings_with(agent_budgets={"FORGE": {"max_requests_per_objective": 1}})
    models, governor = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())

    outcomes = await asyncio.gather(
        *(models.complete(request(), call) for _ in range(3)), return_exceptions=True
    )

    assert len(provider.requests) == 1
    assert sum(isinstance(item, AgentBudgetExceeded) for item in outcomes) == 2
    assert governor.usage(call.objective_id, AgentId.FORGE)[0] == 1


async def test_request_budget_is_enforced_per_agent(database, usage):
    provider = MockProvider(name="mock", models=[ModelInfo(name="m0", tiers=STANDARD)])
    settings = settings_with(agent_budgets={"SCOUT": {"max_requests_per_objective": 2}})
    scout, _ = governed(database, usage, provider, settings, AgentId.SCOUT)
    call = ModelCallContext(objective_id=uuid4())
    await scout.complete(request(), call)
    await scout.complete(request(), call)
    with pytest.raises(AgentBudgetExceeded, match="request budget"):
        await scout.complete(request(), call)
    assert len(provider.requests) == 2


async def test_circuit_breaker_opens_after_consecutive_failures(database, usage):
    provider = MockProvider(
        [ProviderError("mock")] * 10, name="mock", models=[ModelInfo(name="m0", tiers=STANDARD)]
    )
    settings = settings_with(circuit_breaker_failures=3)
    models, _ = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(3):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await models.complete(request(), call)
    assert len(provider.requests) == 3
    [event] = await events_of(database, "provider.circuit_opened")
    assert event.payload["consecutive_failures"] == 3


async def test_success_resets_the_breaker_but_unusable_output_counts(database, usage):
    provider = MockProvider(
        [ProviderError("mock"), "ok", ProviderError("mock"), "not json", "ok"],
        name="mock",
        models=[ModelInfo(name="m0", tiers=STANDARD)],
    )
    settings = settings_with(circuit_breaker_failures=2)
    models, governor = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())
    with pytest.raises(NoModelAvailable):
        await models.complete(request(), call)
    await models.complete(request(), call)
    assert governor.usage(call.objective_id, AgentId.FORGE)[2] == 0
    with pytest.raises(NoModelAvailable):
        await models.complete(request(), call)
    await models.complete(request(), call)
    await models.report_unusable(call)  # the "success" was invalid: two failures in a row
    with pytest.raises(AgentBudgetExceeded):
        await models.complete(request(), call)
    assert len(provider.requests) == 4


async def test_calls_without_an_objective_are_not_governed(database, usage):
    provider = MockProvider(name="mock", models=[ModelInfo(name="m0", tiers=STANDARD)])
    settings = settings_with(agent_budgets={"FORGE": {"max_requests_per_objective": 0}})
    models, _ = governed(database, usage, provider, settings)
    await models.complete(request())
    assert len(provider.requests) == 1


def test_agent_budgets_merge_with_defaults():
    settings = ProviderSettings(
        _env_file=None, agent_budgets={"FORGE": {"max_tokens_per_objective": 5}}
    )
    forge = settings.agent_budget("FORGE")
    assert forge.max_tokens_per_objective == 5
    assert (
        forge.max_requests_per_objective
        == ProviderSettings(_env_file=None).agent_budget("FORGE").max_requests_per_objective
    )
    assert settings.agent_budget("SCOUT") == settings.agent_budgets["default"]
    assert ProviderSettings(_env_file=None).agent_budget("FORGE").max_tokens_per_objective == 20_000


def test_budget_policy_is_unchanged_by_agent_budgets():
    assert BudgetPolicy().max_tokens_per_request == 32_768


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def one_model_provider(script) -> MockProvider:
    return MockProvider(script, name="mock", models=[ModelInfo(name="m0", tiers=STANDARD)])


async def test_usage_is_attributed_to_the_objective(database, usage):
    provider = one_model_provider(["ok"])
    models, _ = governed(database, usage, provider, settings_with())
    call = ModelCallContext(objective_id=uuid4())
    await models.complete(request(), call)
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ProviderUsageRecord)))
    assert [(row.objective_id, row.agent) for row in rows] == [(call.objective_id, "FORGE")]


async def test_request_allowance_survives_a_restart(database, usage):
    settings = settings_with(agent_budgets={"FORGE": {"max_requests_per_objective": 3}})
    provider = one_model_provider(["ok"] * 10)
    models, _ = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())
    await models.complete(request(), call)
    await models.complete(request(), call)

    restarted, governor = governed(database, usage, provider, settings)  # empty in-memory state
    await restarted.complete(request(), call)
    assert governor.usage(call.objective_id, AgentId.FORGE)[0] == 3
    with pytest.raises(AgentBudgetExceeded, match="request budget"):
        await restarted.complete(request(), call)
    assert len(provider.requests) == 3


async def test_token_allowance_survives_a_restart_and_is_per_agent(database, usage):
    settings = settings_with(agent_budgets={"FORGE": {"max_tokens_per_objective": 1000}})
    provider = one_model_provider(["ok"] * 10)
    call = ModelCallContext(objective_id=uuid4())

    # Spend is only seeded from rows of the same objective and agent.
    await usage.record(
        provider="mock", model="m0", agent="FORGE", objective_id=uuid4(), input_tokens=5000
    )
    await usage.record(
        provider="mock",
        model="m0",
        agent="SCOUT",
        objective_id=call.objective_id,
        input_tokens=5000,
    )
    await usage.record(
        provider="mock", model="m0", agent="FORGE", objective_id=call.objective_id, input_tokens=300
    )
    restarted, governor = governed(database, usage, provider, settings)
    await restarted.complete(request(), call)
    requests, tokens, _ = governor.usage(call.objective_id, AgentId.FORGE)
    assert requests == 2 and 300 < tokens < 1000

    await usage.record(
        provider="mock", model="m0", agent="FORGE", objective_id=call.objective_id, input_tokens=900
    )
    again, _ = governed(database, usage, provider, settings)
    with pytest.raises(AgentBudgetExceeded, match="token budget"):
        await again.complete(request(), call)
    assert len(provider.requests) == 1


async def test_open_breaker_survives_a_restart(database, usage):
    settings = settings_with(circuit_breaker_failures=2)
    provider = one_model_provider([ProviderError("mock")] * 10)
    models, _ = governed(database, usage, provider, settings)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(2):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)

    restarted, _ = governed(database, usage, provider, settings)
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await restarted.complete(request(), call)
    assert len(provider.requests) == 2
    other, _ = governed(database, usage, provider, settings)
    with pytest.raises(NoModelAvailable):  # another objective is unaffected
        await other.complete(request(), ModelCallContext(objective_id=uuid4()))


async def test_half_open_probe_failure_reopens_and_restarts_the_cooldown(database, usage):
    clock = FakeClock()
    settings = settings_with(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=60)
    provider = one_model_provider([ProviderError("mock")] * 10)
    models, _ = governed(database, usage, provider, settings, clock=clock)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(2):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)
    clock.advance(59)
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await models.complete(request(), call)

    clock.advance(1)
    with pytest.raises(NoModelAvailable):  # the one probe
        await models.complete(request(), call)
    assert len(provider.requests) == 3
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await models.complete(request(), call)
    assert len(provider.requests) == 3
    assert len(await events_of(database, "provider.circuit_opened")) == 2

    clock.advance(60)  # the cool-down restarted at the failed probe
    with pytest.raises(NoModelAvailable):
        await models.complete(request(), call)
    assert len(provider.requests) == 4


async def test_half_open_probe_success_closes_the_breaker(database, usage):
    clock = FakeClock()
    settings = settings_with(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=60)
    provider = one_model_provider([ProviderError("mock")] * 2 + ["ok"] * 10)
    models, governor = governed(database, usage, provider, settings, clock=clock)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(2):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)
    clock.advance(60)
    await models.complete(request(), call)
    await models.complete(request(), call)
    assert len(provider.requests) == 4
    assert governor.usage(call.objective_id, AgentId.FORGE)[2] == 0
    [closed] = await events_of(database, "provider.circuit_closed")
    assert closed.payload["agent"] == "FORGE"

    # A restart now sees a closed breaker even though an "opened" event exists.
    restarted, _ = governed(database, usage, provider, settings, clock=clock)
    await restarted.complete(request(), call)
    assert len(provider.requests) == 5


async def test_unusable_probe_output_reopens_the_breaker(database, usage):
    clock = FakeClock()
    settings = settings_with(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=60)
    provider = one_model_provider([ProviderError("mock")] * 2 + ["not json"] * 10)
    models, _ = governed(database, usage, provider, settings, clock=clock)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(2):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)
    clock.advance(60)
    await models.complete(request(), call)
    await models.report_unusable(call)
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await models.complete(request(), call)
    assert len(provider.requests) == 3


async def test_cooldown_is_measured_from_the_persisted_event_after_a_restart(database, usage):
    clock = FakeClock()
    settings = settings_with(circuit_breaker_failures=2, circuit_breaker_cooldown_seconds=60)
    provider = one_model_provider([ProviderError("mock")] * 2 + ["ok"] * 10)
    models, _ = governed(database, usage, provider, settings, clock=clock)
    call = ModelCallContext(objective_id=uuid4())
    for _ in range(2):
        with pytest.raises(NoModelAvailable):
            await models.complete(request(), call)

    clock.advance(30)
    restarted, _ = governed(database, usage, provider, settings, clock=clock)
    with pytest.raises(AgentBudgetExceeded, match="circuit breaker"):
        await restarted.complete(request(), call)
    clock.advance(30)
    await restarted.complete(request(), call)  # probe succeeds
    assert len(provider.requests) == 3


async def test_only_one_probe_runs_when_calls_race_after_the_cooldown(database, usage):
    class SlowFailingProvider(MockProvider):
        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            await asyncio.sleep(0.05)
            return await super().complete(request)

    clock = FakeClock()
    settings = settings_with(circuit_breaker_failures=1, circuit_breaker_cooldown_seconds=60)
    provider = SlowFailingProvider(
        [ProviderError("mock")] * 10,
        name="mock",
        models=[ModelInfo(name="m0", tiers=STANDARD)],
    )
    models, _ = governed(database, usage, provider, settings, clock=clock)
    call = ModelCallContext(objective_id=uuid4())
    with pytest.raises(NoModelAvailable):
        await models.complete(request(), call)
    clock.advance(60)
    outcomes = await asyncio.gather(
        *(models.complete(request(), call) for _ in range(3)), return_exceptions=True
    )
    assert len(provider.requests) == 2  # the opening failure plus exactly one probe
    assert sum(isinstance(item, AgentBudgetExceeded) for item in outcomes) == 2


async def test_unreadable_durable_state_fails_closed(database, usage):
    class BrokenSessions:
        def __call__(self):
            raise RuntimeError("database down")

    provider = one_model_provider(["ok"])
    governor = ModelGovernor(settings_with(), sessions=BrokenSessions())
    with pytest.raises(AgentBudgetExceeded, match="usage unavailable"):
        await governor.complete(
            request(agent=AgentId.FORGE, objective_id=uuid4()),
            lambda r, s: ModelRouter([provider], BudgetGuard(usage, settings_with())).complete(
                r, spend=s
            ),
        )
    assert provider.requests == []


def test_breaker_cooldown_defaults_to_thirty_minutes():
    assert ProviderSettings(_env_file=None).circuit_breaker_cooldown_seconds == 1800
