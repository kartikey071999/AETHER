import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from aether.models import (
    BudgetExceeded,
    BudgetGuard,
    BudgetPolicy,
    CompletionRequest,
    ProviderSettings,
)
from aether.storage.models import ProviderUsageRecord


def request(**overrides):
    return CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}],
        provider="groq",
        model="openai/gpt-oss-20b",
        max_tokens=5,
        **overrides,
    )


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (BudgetPolicy(max_requests_per_day=0), "daily requests"),
        (BudgetPolicy(max_tokens_per_day=1), "daily tokens"),
        (BudgetPolicy(max_tokens_per_request=1), "tokens per request"),
        (BudgetPolicy(max_cost_usd_per_day=0), "daily cost"),
    ],
)
async def test_each_cap_blocks_before_call(usage, policy, reason):
    guard = BudgetGuard(usage, ProviderSettings(_env_file=None, budgets={"groq": policy}))
    with pytest.raises(BudgetExceeded, match=reason):
        await guard.reserve("groq", request())


async def test_caps_at_exact_boundaries(usage):
    req = request()
    tokens = req.input_token_estimate() + req.max_tokens
    policy = BudgetPolicy(
        max_tokens_per_request=tokens, max_tokens_per_day=tokens, max_requests_per_day=1
    )
    guard = BudgetGuard(usage, ProviderSettings(_env_file=None, budgets={"groq": policy}))
    await guard.reserve("groq", req)
    with pytest.raises(BudgetExceeded):
        await guard.reserve("groq", req)


async def test_usage_persisted_across_guard_instances(usage):
    settings = ProviderSettings(
        _env_file=None, budgets={"groq": BudgetPolicy(max_requests_per_day=1)}
    )
    await BudgetGuard(usage, settings).reserve("groq", request())
    with pytest.raises(BudgetExceeded, match="daily requests"):
        await BudgetGuard(usage, settings).reserve("groq", request())


async def test_concurrent_reservations_do_not_race(usage):
    settings = ProviderSettings(
        _env_file=None,
        budgets={"groq": BudgetPolicy(max_requests_per_day=1, max_concurrent_requests=10)},
    )
    guards = [BudgetGuard(usage, settings) for _ in range(5)]
    results = await asyncio.gather(
        *(guard.reserve("groq", request()) for guard in guards), return_exceptions=True
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 4


async def test_model_budget_is_additional_to_provider_budget(usage):
    settings = ProviderSettings(
        _env_file=None,
        model_budgets={"groq/openai/gpt-oss-20b": BudgetPolicy(max_requests_per_day=0)},
    )
    with pytest.raises(BudgetExceeded):
        await BudgetGuard(usage, settings).reserve("groq", request())
    other = request().model_copy(update={"model": "openai/gpt-oss-120b"})
    assert (await BudgetGuard(usage, settings).reserve("groq", other)).model == other.model


async def test_provider_budget_covers_all_models(usage):
    settings = ProviderSettings(
        _env_file=None, budgets={"groq": BudgetPolicy(max_requests_per_day=1)}
    )
    guard = BudgetGuard(usage, settings)
    await guard.reserve("groq", request())
    with pytest.raises(BudgetExceeded):
        await guard.reserve("groq", request().model_copy(update={"model": "openai/gpt-oss-120b"}))


async def test_daily_window_is_utc_and_excludes_previous_day(usage, database):
    now = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    await usage.record(provider="groq", model="openai/gpt-oss-20b", input_tokens=500)
    async with database.sessions() as session, session.begin():
        row = await session.scalar(select(ProviderUsageRecord))
        row.occurred_at = now - timedelta(microseconds=1)
    settings = ProviderSettings(
        _env_file=None, budgets={"groq": BudgetPolicy(max_requests_per_day=1)}
    )
    assert await BudgetGuard(usage, settings, clock=lambda: now).reserve("groq", request())


async def test_active_prior_day_reservation_still_counts_concurrency(usage):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    settings = ProviderSettings(
        _env_file=None, budgets={"groq": BudgetPolicy(max_concurrent_requests=1)}
    )
    await BudgetGuard(usage, settings, clock=lambda: yesterday).reserve("groq", request())
    with pytest.raises(BudgetExceeded, match="concurrent"):
        await BudgetGuard(usage, settings).reserve("groq", request())


async def test_persisted_cost_cap_is_enforced(usage):
    await usage.record(provider="groq", model="other", cost_usd=Decimal(10))
    settings = ProviderSettings(_env_file=None)
    with pytest.raises(BudgetExceeded, match="daily cost"):
        await BudgetGuard(usage, settings).reserve("groq", request())


async def test_unconfigured_provider_fails_closed(guard):
    with pytest.raises(BudgetExceeded, match="no budget"):
        await guard.reserve("unknown", request())
