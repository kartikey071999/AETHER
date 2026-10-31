"""Per-minute quotas (rolling 60s windows) and HTTP 429 cool-downs."""

from datetime import UTC, datetime, timedelta

import pytest

from aether.communication.messages import AgentId
from aether.models import (
    BudgetExceeded,
    BudgetGuard,
    BudgetPolicy,
    CompletionRequest,
    MockProvider,
    ModelInfo,
    ModelRouter,
    ModelTier,
    ProviderError,
    ProviderSettings,
)

MODEL = "openai/gpt-oss-20b"
_ALL = frozenset({ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING, ModelTier.PREMIUM})


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def request(**overrides):
    fields = {"provider": "groq", "model": MODEL, "max_tokens": 5, **overrides}
    return CompletionRequest(messages=[{"role": "user", "content": "Hello"}], **fields)


def guard_with(usage, clock, **policy):
    settings = ProviderSettings(
        _env_file=None,
        budgets={"groq": BudgetPolicy(max_requests_per_day=None, max_tokens_per_day=None)},
        model_budgets={f"groq/{MODEL}": BudgetPolicy(**policy)},
    )
    return BudgetGuard(usage, settings, clock=clock)


async def finish_all(guard, reservations):
    for reservation in reservations:
        await guard.finish(
            reservation, request(), response=None, error=None, latency_ms=1, attempt=1
        )


async def test_requests_per_minute_blocks_then_rolls_off(usage):
    clock = Clock()
    guard = guard_with(usage, clock, max_requests_per_minute=2, max_concurrent_requests=10)
    done = [await guard.reserve("groq", request()) for _ in range(2)]
    await finish_all(guard, done)
    with pytest.raises(BudgetExceeded, match="requests per minute"):
        await guard.reserve("groq", request())
    clock.now += timedelta(seconds=61)
    await guard.reserve("groq", request())


async def test_tokens_per_minute_blocks_then_rolls_off(usage):
    clock = Clock()
    req = request()
    per_call = req.input_token_estimate() + req.max_tokens
    guard = guard_with(usage, clock, max_tokens_per_minute=per_call * 2, max_concurrent_requests=10)
    done = [await guard.reserve("groq", req) for _ in range(2)]
    await finish_all(guard, done)
    with pytest.raises(BudgetExceeded, match="tokens per minute"):
        await guard.reserve("groq", req)
    clock.now += timedelta(seconds=61)
    await guard.reserve("groq", req)


async def test_default_groq_safeguard_model_allows_three_requests_per_minute(usage):
    clock = Clock()
    settings = ProviderSettings(_env_file=None)
    guard = BudgetGuard(usage, settings, clock=clock)
    req = request(model="openai/gpt-oss-safeguard-20b")
    done = [await guard.reserve("groq", req) for _ in range(3)]
    await finish_all(guard, done)
    with pytest.raises(BudgetExceeded, match="requests per minute"):
        await guard.reserve("groq", req)


async def test_rate_limited_model_cools_down_and_the_next_model_serves(usage):
    groq = MockProvider(
        [ProviderError("groq", code="http_429")],
        name="groq",
        models=[ModelInfo(name="qwen/qwen3.8-27b", tiers=_ALL)],
    )
    astra = MockProvider(name="azure_openai", models=[ModelInfo(name="gpt-6-astra", tiers=_ALL)])
    settings = ProviderSettings(
        _env_file=None,
        max_retries=0,
        AETHER_MODEL_ASSIGNMENT={"ROGUE": ["groq/qwen/qwen3.8-27b", "azure_openai/gpt-6-astra"]},
    )
    router = ModelRouter([groq, astra], BudgetGuard(usage, settings))
    call = CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}],
        tier=ModelTier.STANDARD,
        agent=AgentId.ROGUE,
    )
    first = await router.complete(call)
    assert (first.provider, len(groq.requests)) == ("azure_openai", 1)
    # One 429 opens the model's circuit at once (no consecutive-failure threshold).
    assert router.circuits.open_models() == ["groq/qwen/qwen3.8-27b"]
    second = await router.complete(call)
    assert second.provider == "azure_openai" and len(groq.requests) == 1
