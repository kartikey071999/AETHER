from decimal import Decimal

import pytest
from model_helpers import records

from aether.models import (
    BudgetGuard,
    CompletionRequest,
    MockProvider,
    ModelRouter,
    ProviderSettings,
)
from aether.observability.recorder import EventRecorder


async def test_accounting_failure_rolls_back_events_and_does_not_fallback(usage, database):
    class FailingRecorder(EventRecorder):
        def add(self, session, event):
            if event.event_type == "provider.request_completed":
                raise RuntimeError("event persistence unavailable")
            return super().add(session, event)

    first = MockProvider()
    fallback = MockProvider(name="groq")
    guard = BudgetGuard(usage, ProviderSettings(_env_file=None), FailingRecorder())
    with pytest.raises(RuntimeError, match="persistence unavailable"):
        await ModelRouter([first, fallback], guard).complete(
            CompletionRequest(messages=[{"role": "user", "content": "Hello"}], max_tokens=5)
        )
    rows, events = await records(database)
    assert len(rows) == 1 and rows[0].status == "reserved"
    assert not events and not fallback.requests


async def test_tiny_costs_round_up_instead_of_evading_cost_accounting(usage, database):
    from aether.models import ModelInfo, ModelTier

    mock = MockProvider(
        name="groq",
        models=[ModelInfo(name="openai/gpt-oss-20b", tiers=frozenset({ModelTier.LIGHT}))],
    )
    await ModelRouter([mock], BudgetGuard(usage, ProviderSettings(_env_file=None))).complete(
        CompletionRequest(
            messages=[{"role": "user", "content": "Hello"}],
            tier=ModelTier.LIGHT,
            max_tokens=1,
        )
    )
    rows, events = await records(database)
    assert rows[0].cost_usd == Decimal("0.000002")
    assert events[0].payload["cost"] == float(rows[0].cost_usd)
