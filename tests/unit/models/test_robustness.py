import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from model_helpers import records
from pydantic import ValidationError
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetExceeded,
    BudgetGuard,
    BudgetPolicy,
    CompletionRequest,
    CompletionResponse,
    EmptyCompletion,
    GroqProvider,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    OllamaProvider,
    ProviderError,
    ProviderSettings,
    reconcile_stale_reservations,
)
from aether.models.costs import estimate_cost
from aether.observability.recorder import EventRecorder
from aether.storage.models import EventRecord, ProviderUsageRecord


def request(**overrides):
    return CompletionRequest(
        **{
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 5,
            **overrides,
        }
    )


def response(**overrides):
    return CompletionResponse(
        **{
            "text": " \n\t",
            "provider": "mock",
            "model": "mock-model",
            "input_tokens": 7,
            "output_tokens": 128,
            "latency_ms": 0,
            "finish_reason": "length",
            **overrides,
        }
    )


@pytest.mark.parametrize("estimated", [False, True])
async def test_empty_completion_falls_back_with_usage_and_event(guard, database, estimated):
    # A blank completion that did not hit the length limit is not retried on the same model.
    empty = response(usage_estimated=estimated, finish_reason="stop")
    first = MockProvider(
        [empty],
        models=[
            ModelInfo(name="mock-model", tiers={ModelTier.STANDARD}),
            ModelInfo(name="fallback", tiers={ModelTier.STANDARD}),
        ],
    )
    result = await ModelRouter([first], guard).complete(request())
    assert result.model == "fallback"
    assert len(first.requests) == 2
    rows, events = await records(database)
    failed = next(row for row in rows if row.status == "failed")
    assert (failed.input_tokens, failed.output_tokens) == (7, 128)
    event = next(event for event in events if event.payload["status"] == "failed")
    assert event.payload["error_type"] == "EmptyCompletion"
    assert event.payload["usage_estimated"] is estimated
    assert event.payload["tokens"] == 135
    async with database.sessions() as session:
        usage_events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "provider.usage_recorded")
            )
        )
    assert len(usage_events) == 2
    assert all("text" not in event.payload for event in events + usage_events)


async def test_empty_completion_is_typed_and_preserves_safe_metadata():
    with pytest.raises(EmptyCompletion) as captured:
        await MockProvider([response()]).complete(request(model="mock-model"))
    assert isinstance(captured.value, ProviderError)
    assert not captured.value.transient
    assert captured.value.finish_reason == "length"
    assert (captured.value.input_tokens, captured.value.output_tokens) == (7, 128)


async def test_router_rejects_empty_response_from_custom_provider(guard, database):
    class UncheckedProvider(MockProvider):
        async def complete(self, req):
            return response()

    with pytest.raises(NoModelAvailable, match="EmptyCompletion"):
        await ModelRouter([UncheckedProvider()], guard).complete(request())
    assert (await records(database))[0][0].output_tokens == 128


async def test_nonempty_truncated_response_is_preserved(guard):
    result = await ModelRouter([MockProvider([response(text="partial")])], guard).complete(
        request()
    )
    assert result.text == "partial" and result.finish_reason == "length"


@pytest.mark.parametrize(
    ("provider_type", "model", "tier"),
    [
        (GroqProvider, "openai/gpt-oss-20b", ModelTier.LIGHT),
        (AzureOpenAIProvider, "gpt-6-astra", ModelTier.PREMIUM),
        (AzureAnthropicProvider, "claude-opus-4-8", ModelTier.PREMIUM),
        (OllamaProvider, "local", ModelTier.STANDARD),
    ],
)
async def test_remote_providers_reject_empty_text(permissions, provider_type, model, tier):
    settings = ProviderSettings(
        _env_file=None,
        GROQ_API_KEY="test",
        AZURE_OPENAI_ENDPOINT="https://azure.example.com",
        AZURE_OPEN_AI_KEY="test",
        OPENAI_API_VERSION="test",
        ANTHROPIC_ENDPOINT="https://opus.example.com",
        ANTHROPIC_API_KEY="test",
        API_VERSION="test",
        ollama_models={"local": [ModelTier.STANDARD]},
    )
    if provider_type is OllamaProvider:
        provider = OllamaProvider(
            settings,
            permissions,
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=(
                        {"models": [{"name": model}]}
                        if req.url.path == "/api/tags"
                        else {
                            "message": {"content": " \n"},
                            "prompt_eval_count": 7,
                            "eval_count": 128,
                            "done_reason": "length",
                        }
                    ),
                )
            ),
        )
    else:
        data = (
            {
                "content": [{"type": "thinking", "text": "not an answer"}],
                "usage": {"input_tokens": 7, "output_tokens": 128},
                "stop_reason": "max_tokens",
            }
            if provider_type is AzureAnthropicProvider
            else {
                "choices": [{"message": {"content": None}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 128},
            }
        )
        create = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: data))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            messages=SimpleNamespace(create=create),
            close=AsyncMock(),
        )
        provider = provider_type(settings, permissions, client=client)
    try:
        with pytest.raises(EmptyCompletion) as captured:
            await provider.complete(request(model=model, tier=tier))
        assert (captured.value.input_tokens, captured.value.output_tokens) == (7, 128)
        assert not captured.value.usage_estimated
    finally:
        await provider.aclose()


async def test_empty_sdk_completion_records_measured_cost_and_retries_with_more_tokens(
    permissions, usage, database
):
    data = {
        "choices": [{"message": {"content": None}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 128},
    }
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(model_dump=lambda: data),
            SimpleNamespace(
                model_dump=lambda: {
                    **data,
                    "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                }
            ),
        ]
    )
    settings = ProviderSettings(_env_file=None, GROQ_API_KEY="test", max_retries=2)
    provider = GroqProvider(
        settings,
        permissions,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    result = await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
        request(agent=AgentId.MUSE)
    )
    # The length-truncated empty reply is retried once on the same model with doubled tokens.
    assert result.model == "openai/gpt-oss-20b"
    assert [call.kwargs["max_tokens"] for call in create.await_args_list] == [128, 256]
    assert all(call.kwargs["reasoning_effort"] == "low" for call in create.await_args_list)
    rows, events = await records(database)
    failed = next(row for row in rows if row.status == "failed")
    assert failed.cost_usd == Decimal("0.000039")
    assert len(events) == 2 and not events[0].payload["usage_estimated"]


@pytest.mark.parametrize("requested", [5, 128, 200])
async def test_model_minimum_raises_request_without_lowering_larger_values(usage, requested):
    model = ModelInfo(name="reasoning", tiers={ModelTier.STANDARD}, min_max_tokens=128)
    provider = MockProvider(models=[model])
    settings = ProviderSettings(_env_file=None)
    req = request(max_tokens=requested)
    await ModelRouter([provider], BudgetGuard(usage, settings)).complete(req)
    assert provider.requests[0].max_tokens == max(requested, 128)
    assert req.max_tokens == requested


@pytest.mark.parametrize("cap_source", ["request", "provider", "model"])
async def test_minimum_conflict_is_clear_and_does_not_call(usage, database, cap_source):
    model = ModelInfo(name="reasoning", tiers={ModelTier.STANDARD}, min_max_tokens=128)
    provider = MockProvider(models=[model])
    req = request(max_tokens_cap=127 if cap_source == "request" else None)
    budget = BudgetPolicy(max_tokens_per_request=req.input_token_estimate() + 127)
    settings = ProviderSettings(
        _env_file=None,
        budgets={"mock": budget} if cap_source == "provider" else {},
        model_budgets={"mock/reasoning": budget} if cap_source == "model" else {},
    )
    with pytest.raises(NoModelAvailable, match=r"model minimum=128.*cap=127"):
        await ModelRouter([provider], BudgetGuard(usage, settings)).complete(req)
    assert not provider.requests
    assert await records(database) == ([], [])


async def test_minimum_at_exact_output_and_total_cap_is_allowed(usage):
    provider = MockProvider(
        models=[ModelInfo(name="reasoning", tiers={ModelTier.STANDARD}, min_max_tokens=128)]
    )
    req = request(max_tokens_cap=128)
    settings = ProviderSettings(
        _env_file=None,
        budgets={"mock": {"max_tokens_per_request": req.input_token_estimate() + 128}},
    )
    await ModelRouter([provider], BudgetGuard(usage, settings)).complete(req)
    assert provider.requests[0].max_tokens == 128


def test_price_override_env_parsing_and_precision(monkeypatch):
    monkeypatch.setenv(
        "AETHER_PROVIDER_PRICES",
        '{"groq/openai/gpt-oss-20b":{"input_per_mtok":"1.23456789","output_per_mtok":2}}',
    )
    settings = ProviderSettings(_env_file=None)
    cost, unknown = estimate_cost(
        "groq", "openai/gpt-oss-20b", 1_000_000, 1_000_000, prices=settings.prices
    )
    assert cost == Decimal("3.23456789") and not unknown


@pytest.mark.parametrize(
    "value",
    [
        {"input_per_mtok": -1, "output_per_mtok": 1},
        {"input_per_mtok": "NaN", "output_per_mtok": 1},
        {"input_per_mtok": 1, "output_per_mtok": float("inf")},
        {"input_per_mtok": 1},
        {"input_per_mtok": 1, "output_per_mtok": 2, "unknown": 3},
    ],
)
def test_invalid_prices_are_rejected(value):
    with pytest.raises(ValidationError):
        ProviderSettings(_env_file=None, prices={"groq/model": value})


@pytest.mark.parametrize(
    "key", ["model", "/model", "groq/", " groq/model", "groq/model ", "groq /model", "groq/ model"]
)
def test_invalid_price_keys_are_rejected(key):
    with pytest.raises(ValidationError, match="provider/model"):
        ProviderSettings(_env_file=None, prices={key: {"input_per_mtok": 1, "output_per_mtok": 1}})


async def test_overrides_apply_to_routing_reservation_response_and_ledger(usage, database):
    settings = ProviderSettings(
        _env_file=None,
        # No assignment: the final fallback orders models by configured price.
        AETHER_MODEL_ASSIGNMENT={"ORCHESTR": []},
        prices={
            "groq/openai/gpt-oss-120b": {"input_per_mtok": 10, "output_per_mtok": 10},
            "groq/qwen/qwen3.8-27b": {"input_per_mtok": 1, "output_per_mtok": 2},
        },
    )
    models = [
        ModelInfo(name=name, tiers={ModelTier.REASONING})
        for name in ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")
    ]
    guard = BudgetGuard(usage, settings)
    reservation = await guard.reserve("groq", request(provider="groq", model="qwen/qwen3.8-27b"))
    expected = (Decimal(request().input_token_estimate()) + 10) / 1_000_000
    assert reservation.cost_usd == expected
    await guard.finish(
        reservation, request(), response=None, error=ProviderError("groq"), latency_ms=0, attempt=1
    )
    result = await ModelRouter([MockProvider(name="groq", models=models)], guard).complete(
        request(tier=ModelTier.REASONING)
    )
    assert result.model == "qwen/qwen3.8-27b"
    assert result.cost_usd == pytest.approx(0.000020)
    rows, events = await records(database)
    succeeded = next(row for row in rows if row.status == "succeeded")
    assert succeeded.cost_usd == Decimal("0.000020")
    assert not next(event for event in events if event.payload["status"] == "succeeded").payload[
        "cost_estimate_unknown"
    ]


@pytest.mark.parametrize("provider", ["azure_openai", "azure_anthropic", "groq"])
@pytest.mark.parametrize("allow", [False, True])
async def test_unknown_price_usd_ceiling_requires_provider_opt_in(usage, database, provider, allow):
    settings = ProviderSettings(_env_file=None, budgets={provider: {"allow_unpriced": allow}})
    guard = BudgetGuard(usage, settings)
    req = request(provider=provider, model="unpriced")
    if allow:
        reservation = await guard.reserve(provider, req)
        assert reservation.cost_estimate_unknown and reservation.cost_usd == 0
    else:
        with pytest.raises(BudgetExceeded, match="price unknown.*USD ceiling"):
            await guard.reserve(provider, req)
        assert await records(database) == ([], [])


async def test_model_usd_ceiling_also_refuses_unknown_price(usage):
    settings = ProviderSettings(
        _env_file=None,
        budgets={"groq": {"max_cost_usd_per_day": None}},
        model_budgets={"groq/unpriced": {"max_cost_usd_per_day": 1, "allow_unpriced": True}},
    )
    with pytest.raises(BudgetExceeded, match="price unknown"):
        await BudgetGuard(usage, settings).reserve("groq", request(model="unpriced"))


async def test_unknown_price_without_any_usd_ceiling_is_allowed(usage):
    settings = ProviderSettings(_env_file=None, budgets={"groq": {"max_cost_usd_per_day": None}})
    assert (
        await BudgetGuard(usage, settings).reserve("groq", request(model="unpriced"))
    ).cost_estimate_unknown


@pytest.mark.parametrize(
    "cap",
    [
        {"max_tokens_per_request": 1},
        {"max_tokens_per_day": 0},
        {"max_requests_per_day": 0},
    ],
)
async def test_allow_unpriced_never_bypasses_token_or_request_caps(usage, cap):
    settings = ProviderSettings(
        _env_file=None, budgets={"azure_openai": {"allow_unpriced": True, **cap}}
    )
    with pytest.raises(BudgetExceeded, match="exceeded"):
        await BudgetGuard(usage, settings).reserve("azure_openai", request(model="unpriced"))


async def test_configured_azure_price_enables_default_usd_budget(usage):
    settings = ProviderSettings(
        _env_file=None,
        prices={"azure_openai/gpt-6-astra": {"input_per_mtok": 1, "output_per_mtok": 2}},
    )
    reservation = await BudgetGuard(usage, settings).reserve(
        "azure_openai", request(model="gpt-6-astra")
    )
    assert not reservation.cost_estimate_unknown and reservation.cost_usd > 0


async def test_reconciliation_boundaries_conservative_daily_charge_and_idempotence(usage, database):
    now = datetime(2026, 10, 31, 0, 0, 45, tzinfo=UTC)
    margin = timedelta(seconds=60)
    settings = ProviderSettings(
        _env_file=None, budgets={"groq": {"max_concurrent_requests": 10, "max_requests_per_day": 4}}
    )
    reservations = []
    for age in (
        timedelta(days=1),
        margin + timedelta(microseconds=1),
        margin,
        margin - timedelta(microseconds=1),
    ):
        reservations.append(
            await BudgetGuard(usage, settings, clock=lambda age=age: now - age).reserve(
                "groq", request(model="openai/gpt-oss-20b")
            )
        )
    summary = await reconcile_stale_reservations(database.sessions, older_than=margin, now=now)
    assert summary.abandoned == 2
    assert summary.input_tokens == 2 * request().input_token_estimate()
    assert summary.output_tokens == 10
    assert summary.cost_usd == sum(row.cost_usd for row in reservations[:2])
    async with database.sessions() as session:
        rows = {row.id: row for row in await session.scalars(select(ProviderUsageRecord))}
        events = list(await session.scalars(select(EventRecord)))
        totals = await usage.totals_in(
            session,
            provider="groq",
            since=now.replace(hour=0, minute=0, second=0, microsecond=0),
            until=now + timedelta(days=1),
        )
    assert [rows[row.id].status for row in reservations] == [
        "abandoned",
        "abandoned",
        "reserved",
        "reserved",
    ]
    assert len(events) == 2
    assert all(event.payload["status"] == "abandoned" for event in events)
    assert all(event.aggregate_id in {row.id for row in reservations[:2]} for event in events)
    assert totals.requests == 2 and totals.active == 2
    repeated = await reconcile_stale_reservations(database.sessions, older_than=margin, now=now)
    assert repeated.abandoned == 0 and repeated.cost_usd == 0
    await BudgetGuard(usage, settings, clock=lambda: now).reserve(
        "groq", request(model="openai/gpt-oss-20b")
    )
    await BudgetGuard(usage, settings, clock=lambda: now).reserve(
        "groq", request(model="openai/gpt-oss-20b")
    )
    with pytest.raises(BudgetExceeded, match="daily requests"):
        await BudgetGuard(usage, settings, clock=lambda: now).reserve(
            "groq", request(model="openai/gpt-oss-20b")
        )


async def test_concurrent_reconciliation_is_idempotent_and_releases_slots(usage, database):
    now = datetime.now(UTC)
    settings = ProviderSettings(_env_file=None, budgets={"groq": {"max_concurrent_requests": 1}})
    guard = BudgetGuard(usage, settings, clock=lambda: now - timedelta(minutes=2))
    reservation = await guard.reserve("groq", request(model="openai/gpt-oss-20b"))
    summaries = await asyncio.gather(
        *(
            reconcile_stale_reservations(
                database.sessions, older_than=timedelta(seconds=60), now=now
            )
            for _ in range(3)
        )
    )
    assert sum(summary.abandoned for summary in summaries) == 1
    async with database.sessions() as session:
        assert (await session.get(ProviderUsageRecord, reservation.id)).status == "abandoned"
        assert len(list(await session.scalars(select(EventRecord)))) == 1
    assert await BudgetGuard(usage, settings, clock=lambda: now).reserve(
        "groq", request(model="openai/gpt-oss-20b")
    )


@pytest.mark.parametrize(
    ("older_than", "now"),
    [
        (timedelta(0), datetime.now(UTC)),
        (timedelta(seconds=-1), datetime.now(UTC)),
        (timedelta(seconds=60), datetime(2026, 10, 31)),
    ],
)
async def test_reconciliation_rejects_unsafe_time_inputs(database, older_than, now):
    with pytest.raises(ValueError):
        await reconcile_stale_reservations(database.sessions, older_than=older_than, now=now)


async def test_missing_usage_on_empty_sdk_response_keeps_conservative_reservation(
    permissions, usage, database
):
    data = {"choices": [{"message": {"content": " \t"}, "finish_reason": "length"}]}
    create = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: data))
    settings = ProviderSettings(_env_file=None, GROQ_API_KEY="test", max_retries=2)
    provider = GroqProvider(
        settings,
        permissions,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    with pytest.raises(NoModelAvailable, match="EmptyCompletion"):
        await ModelRouter([provider], BudgetGuard(usage, settings)).complete(
            request(model="openai/gpt-oss-20b")
        )
    # One bounded length retry with a doubled budget, then no further attempts.
    assert [call.kwargs["max_tokens"] for call in create.await_args_list] == [128, 256]
    rows, events = await records(database)
    assert rows[0].input_tokens == request().input_token_estimate()
    assert rows[0].output_tokens == 128
    assert rows[0].cost_usd > 0 and events[0].payload["usage_estimated"]


async def test_direct_provider_honors_price_override_and_explicit_token_cap(permissions):
    data = {
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }
    create = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: data))
    settings = ProviderSettings(
        _env_file=None,
        GROQ_API_KEY="test",
        prices={"groq/openai/gpt-oss-20b": {"input_per_mtok": 1, "output_per_mtok": 2}},
    )
    provider = GroqProvider(
        settings,
        permissions,
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    result = await provider.complete(request(model="openai/gpt-oss-20b", tier=ModelTier.LIGHT))
    assert result.cost_usd == pytest.approx(0.000011)
    assert not result.cost_estimate_unknown
    create.reset_mock()
    with pytest.raises(ProviderError, match=r"minimum=128.*cap=127"):
        await provider.complete(
            request(model="openai/gpt-oss-20b", tier=ModelTier.LIGHT, max_tokens_cap=127)
        )
    create.assert_not_awaited()


async def test_reconciliation_preserves_finalized_rows_and_rolls_back_failed_event(
    usage, database, monkeypatch
):
    from aether.models import budgets

    now = datetime.now(UTC)
    settings = ProviderSettings(_env_file=None)
    guard = BudgetGuard(usage, settings, clock=lambda: now - timedelta(minutes=2))
    req = request(model="openai/gpt-oss-20b")
    finalized = await guard.reserve("groq", req)
    await guard.finish(
        finalized, req, response=None, error=ProviderError("groq"), latency_ms=0, attempt=1
    )
    stale = await guard.reserve("groq", req)

    class FailingRecorder(EventRecorder):
        def add(self, session, event):
            raise RuntimeError("event persistence unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(budgets, "EventRecorder", FailingRecorder)
        with pytest.raises(RuntimeError, match="persistence unavailable"):
            await reconcile_stale_reservations(
                database.sessions, older_than=timedelta(seconds=60), now=now
            )
    async with database.sessions() as session:
        rows = {row.id: row for row in await session.scalars(select(ProviderUsageRecord))}
        assert rows[stale.id].status == "reserved"
        assert rows[finalized.id].status == "failed"
        assert len(list(await session.scalars(select(EventRecord)))) == 2
    summary = await reconcile_stale_reservations(
        database.sessions, older_than=timedelta(seconds=60), now=now
    )
    assert summary.abandoned == 1
    async with database.sessions() as session:
        row = await session.get(ProviderUsageRecord, finalized.id)
        assert row.status == "failed"
        assert row.occurred_at.replace(tzinfo=UTC) == now - timedelta(minutes=2)


@pytest.mark.parametrize("allow", [False, True])
async def test_router_unknown_azure_price_is_fail_closed_unless_allowed(usage, database, allow):
    settings = ProviderSettings(_env_file=None, budgets={"azure_openai": {"allow_unpriced": allow}})
    provider = MockProvider(
        name="azure_openai",
        models=[ModelInfo(name="gpt-6-astra", tiers={ModelTier.PREMIUM}, min_max_tokens=128)],
    )
    router = ModelRouter([provider], BudgetGuard(usage, settings))
    req = request(tier=ModelTier.PREMIUM)
    if allow:
        result = await router.complete(req)
        assert result.cost_estimate_unknown and result.cost_usd == 0
        assert provider.requests[0].max_tokens == 128
        assert (await records(database))[1][0].payload["cost_estimate_unknown"]
    else:
        with pytest.raises(NoModelAvailable, match="price unknown.*USD ceiling"):
            await router.complete(req)
        assert not provider.requests and await records(database) == ([], [])
