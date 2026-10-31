import asyncio

import pytest
from model_helpers import records
from sqlalchemy import select

from aether.models import (
    BudgetGuard,
    BudgetPolicy,
    CompletionRequest,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    ProviderError,
    ProviderSettings,
)
from aether.storage.models import EventRecord


def request(tier=ModelTier.STANDARD, **overrides):
    return CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}], tier=tier, **overrides
    )


def provider(name, tiers, *, responses=None, available=True):
    return MockProvider(
        responses,
        name=name,
        models=[ModelInfo(name=f"{name}-model", tiers=frozenset(tiers))],
        available=available,
    )


@pytest.mark.parametrize("tier", [ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING])
async def test_local_first_by_tier(guard, tier):
    local = provider("ollama", [tier])
    groq = provider("groq", [tier])
    router = ModelRouter([groq, local], guard)
    assert (await router.complete(request(tier))).provider == "ollama"
    assert not groq.requests


async def test_unavailable_and_unsuitable_providers_are_skipped(guard):
    local = provider("ollama", [ModelTier.LIGHT])
    absent = provider("absent", [ModelTier.STANDARD], available=False)
    groq = provider("groq", [ModelTier.STANDARD])
    router = ModelRouter([local, absent, groq], guard)
    assert (await router.complete(request())).provider == "groq"
    assert not local.requests and not absent.requests


@pytest.mark.parametrize("tier", [ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING])
async def test_azure_serves_nonpremium_tiers_when_nothing_cheaper_exists(guard, tier):
    # The assignment, not a blanket premium-only rule, decides when Azure is used.
    astra = provider("azure_openai", [tier])
    opus = provider("azure_anthropic", [tier])
    result = await ModelRouter([astra, opus], guard).complete(request(tier))
    # The final fallback prefers the unlimited Azure deployments, Azure OpenAI first.
    assert result.provider == "azure_openai" and not opus.requests


async def test_none_rejects_without_usage(guard, database):
    mock = MockProvider()
    with pytest.raises(ValueError, match="deterministic"):
        await ModelRouter([mock], guard).complete(request(ModelTier.NONE))
    assert await records(database) == ([], [])
    assert not mock.requests


async def test_groq_only_after_both_azure_fallbacks_fail(usage, settings, database):
    # Groq's small per-model quotas make it the last resort of the final fallback.
    groq = provider("groq", [ModelTier.PREMIUM])
    opus = provider(
        "azure_anthropic", [ModelTier.PREMIUM], responses=[ProviderError("azure_anthropic")]
    )
    astra = provider("azure_openai", [ModelTier.PREMIUM], responses=[ProviderError("azure_openai")])
    guard = BudgetGuard(usage, settings.model_copy(update={"max_models_per_request": 3}))
    result = await ModelRouter([groq, opus, astra], guard).complete(request(ModelTier.PREMIUM))
    assert result.provider == "groq"
    assert len(astra.requests) == len(opus.requests) == len(groq.requests) == 1
    rows, events = await records(database)
    assert len(rows) == len(events) == 3
    assert sorted(row.status for row in rows) == ["failed", "failed", "succeeded"]
    assert sorted(event.payload["status"] for event in events) == ["failed", "failed", "succeeded"]


async def test_astra_explicit_selection_does_not_call_cheaper_providers(guard):
    groq = provider("groq", [ModelTier.PREMIUM])
    astra = provider("azure_openai", [ModelTier.PREMIUM])
    result = await ModelRouter([groq, astra], guard).complete(
        request(ModelTier.PREMIUM, provider="azure_openai")
    )
    assert result.provider == "azure_openai" and not groq.requests


async def test_budget_exceeded_falls_back_without_calling_provider(usage, database):
    settings = ProviderSettings(
        _env_file=None,
        max_retries=0,
        budgets={"ollama": BudgetPolicy(max_requests_per_day=0)},
        prices={"groq/groq-model": {"input_per_mtok": 0, "output_per_mtok": 0}},
    )
    local = provider("ollama", [ModelTier.STANDARD])
    groq = provider("groq", [ModelTier.STANDARD])
    result = await ModelRouter([local, groq], BudgetGuard(usage, settings)).complete(request())
    assert result.provider == "groq" and not local.requests
    rows, events = await records(database)
    assert len(rows) == len(events) == 1


async def test_cheapest_model_for_tier_selected(guard):
    groq = MockProvider(
        name="groq",
        models=[
            ModelInfo(name="qwen/qwen3.8-27b", tiers=frozenset({ModelTier.REASONING})),
            ModelInfo(name="openai/gpt-oss-120b", tiers=frozenset({ModelTier.REASONING})),
        ],
    )
    result = await ModelRouter([groq], guard).complete(request(ModelTier.REASONING))
    assert result.model == "openai/gpt-oss-120b"
    assert result.cost_usd == pytest.approx((10 * 0.15 + 5 * 0.60) / 1_000_000)
    assert not result.cost_estimate_unknown


async def test_retries_bounded_and_recorded_each_attempt(usage, database):
    sleeps = []

    async def sleeper(delay):
        sleeps.append(delay)

    settings = ProviderSettings(
        _env_file=None, max_retries=2, retry_backoff_seconds=0.5, retry_max_delay_seconds=0.75
    )
    mock = MockProvider([ProviderError("mock", transient=True)] * 3)
    with pytest.raises(NoModelAvailable):
        await ModelRouter([mock], BudgetGuard(usage, settings), sleeper=sleeper).complete(request())
    assert sleeps == [0.5, 0.75]
    assert len(mock.requests) == 3
    rows, events = await records(database)
    assert len(rows) == len(events) == 3
    assert all(row.status == "failed" for row in rows)
    assert sorted(event.payload["attempt"] for event in events) == [1, 2, 3]
    assert all(event.payload["usage_estimated"] for event in events)


async def test_retry_cannot_bypass_request_budget(usage, database):
    settings = ProviderSettings(
        _env_file=None,
        max_retries=2,
        retry_backoff_seconds=0,
        budgets={"mock": BudgetPolicy(max_requests_per_day=1)},
    )
    mock = MockProvider([ProviderError("mock", transient=True)])
    with pytest.raises(NoModelAvailable):
        await ModelRouter([mock], BudgetGuard(usage, settings)).complete(request())
    assert len(mock.requests) == 1
    assert len((await records(database))[0]) == 1


async def test_success_reconciles_actual_tokens_and_events(guard, database):
    await ModelRouter([MockProvider()], guard).complete(request())
    rows, events = await records(database)
    assert len(rows) == len(events) == 1
    assert rows[0].input_tokens == 10 and rows[0].output_tokens == 5
    assert events[0].payload["tokens"] == 15
    assert events[0].aggregate_id == rows[0].id
    async with database.sessions() as session:
        usage_events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "provider.usage_recorded")
            )
        )
    assert len(usage_events) == 1


async def test_unexpected_errors_are_recorded_and_reraised_without_fallback(guard, database):
    broken = MockProvider([RuntimeError("internal invariant")])
    fallback = provider("groq", [ModelTier.STANDARD])
    with pytest.raises(RuntimeError, match="invariant"):
        await ModelRouter([broken, fallback], guard).complete(request())
    assert not fallback.requests
    rows, events = await records(database)
    assert rows[0].status == events[0].payload["status"] == "failed"
    assert events[0].payload["error_type"] == "RuntimeError"


async def test_global_semaphore_limits_inflight_calls(usage):
    active = 0
    peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingProvider(MockProvider):
        async def complete(self, req):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release.wait()
                return await super().complete(req)
            finally:
                active -= 1

    settings = ProviderSettings(
        _env_file=None, budgets={"mock": BudgetPolicy(max_concurrent_requests=10)}
    )
    router = ModelRouter(
        [WaitingProvider()], BudgetGuard(usage, settings), max_concurrent_llm_requests=1
    )
    tasks = [asyncio.create_task(router.complete(request())) for _ in range(4)]
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.sleep(0.02)
    assert peak == 1
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 1


async def test_cancelled_call_records_usage_and_releases_reservation(guard, database):
    entered = asyncio.Event()

    class WaitingProvider(MockProvider):
        async def complete(self, req):
            entered.set()
            await asyncio.Event().wait()

    router = ModelRouter([WaitingProvider()], guard)
    task = asyncio.create_task(router.complete(request()))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows, events = await records(database)
    assert rows[0].status == events[0].payload["status"] == "cancelled"
    assert not router.semaphore.locked()


async def test_timeout_retried_and_persisted(usage, database):
    class TimeoutProvider(MockProvider):
        async def complete(self, req):
            self.requests.append(req)
            raise TimeoutError

    mock = TimeoutProvider()
    settings = ProviderSettings(_env_file=None, max_retries=1, retry_backoff_seconds=0)
    with pytest.raises(NoModelAvailable):
        await ModelRouter([mock], BudgetGuard(usage, settings)).complete(request())
    assert len(mock.requests) == len((await records(database))[0]) == 2
