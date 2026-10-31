import pytest
from model_helpers import records
from pydantic import ValidationError

from aether.communication.messages import AgentId
from aether.models import (
    BudgetGuard,
    CompletionRequest,
    MockProvider,
    ModelAssignment,
    ModelInfo,
    ModelRouter,
    ModelTier,
    NoModelAvailable,
    ProviderError,
    ProviderSettings,
    default_model_assignments,
)

ASTRA = ("azure_openai", "gpt-6-astra")
OPUS = ("azure_anthropic", "claude-opus-4-8")
GROQ_20B = ("groq", "openai/gpt-oss-20b")
GROQ_120B = ("groq", "openai/gpt-oss-120b")
QWEN = ("groq", "qwen/qwen3.8-27b")
_GENERAL = frozenset({ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING, ModelTier.PREMIUM})
_REASONING = frozenset({ModelTier.STANDARD, ModelTier.REASONING, ModelTier.PREMIUM})
_LIGHT = frozenset({ModelTier.LIGHT, ModelTier.STANDARD})


def request(agent, tier=ModelTier.STANDARD, **overrides):
    return CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}], tier=tier, agent=agent, **overrides
    )


class Fleet:
    """The real provider/model catalogue, served offline by mock providers."""

    def __init__(self, *, unavailable=(), responses=None) -> None:
        responses = responses or {}
        self.astra = MockProvider(
            responses.get("azure_openai"),
            name="azure_openai",
            models=[ModelInfo(name="gpt-6-astra", tiers=_GENERAL)],
            available="azure_openai" not in unavailable,
        )
        self.opus = MockProvider(
            responses.get("azure_anthropic"),
            name="azure_anthropic",
            models=[ModelInfo(name="claude-opus-4-8", tiers=_GENERAL)],
            available="azure_anthropic" not in unavailable,
        )
        self.groq = MockProvider(
            responses.get("groq"),
            name="groq",
            models=[
                ModelInfo(name="openai/gpt-oss-20b", tiers=_LIGHT),
                ModelInfo(name="openai/gpt-oss-120b", tiers=_REASONING),
                ModelInfo(name="qwen/qwen3.8-27b", tiers=_REASONING),
            ],
            available="groq" not in unavailable,
        )

    @property
    def providers(self):
        return [self.groq, self.astra, self.opus]

    def router(self, usage, settings, **kwargs) -> ModelRouter:
        return ModelRouter(self.providers, BudgetGuard(usage, settings), **kwargs)


def settings(**overrides) -> ProviderSettings:
    return ProviderSettings(_env_file=None, max_retries=0, **overrides)


# Mechanism tests (circuits, caps) pin an explicit Astra -> Groq chain so they do not depend on
# the default assignment table.
TWO_STEP = {"MUSE": ["azure_openai/gpt-6-astra", "groq/openai/gpt-oss-20b"]}


def two_step(**overrides) -> ProviderSettings:
    return settings(AETHER_MODEL_ASSIGNMENT=TWO_STEP, **overrides)


def served(response) -> tuple[str, str]:
    return response.provider, response.model


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("agent", "tier", "expected"),
    [
        (AgentId.ORCHESTR, ModelTier.STANDARD, ASTRA),
        (AgentId.SCOUT, ModelTier.STANDARD, ASTRA),
        (AgentId.ROGUE, ModelTier.REASONING, OPUS),
        (AgentId.FORGE, ModelTier.REASONING, OPUS),
        (AgentId.WARDEN, ModelTier.STANDARD, OPUS),
        (AgentId.LEDGER, ModelTier.LIGHT, ASTRA),
        (AgentId.MUSE, ModelTier.STANDARD, OPUS),
    ],
)
async def test_each_agent_uses_its_first_assigned_model(usage, database, agent, tier, expected):
    fleet = Fleet()
    response = await fleet.router(usage, settings()).complete(request(agent, tier))
    assert served(response) == expected
    rows, events = await records(database)
    assert rows[0].agent == agent.value
    payload = events[0].payload
    assert payload["assignment"] == "assigned" and payload["assignment_rank"] == 1
    assert payload["skipped"] == []


async def test_light_only_entry_is_skipped_for_standard_work(usage, database):
    config = settings(
        AETHER_MODEL_ASSIGNMENT={
            "LEDGER": [
                {"provider": "groq", "model": "openai/gpt-oss-20b", "tiers": ["light"]},
                "azure_openai/gpt-6-astra",
            ]
        }
    )
    fleet = Fleet()
    response = await fleet.router(usage, config).complete(request(AgentId.LEDGER))
    assert served(response) == ASTRA and not fleet.groq.requests
    _, events = await records(database)
    payload = events[0].payload
    assert payload["assignment_rank"] == 2
    assert payload["skipped"] == [
        {
            "provider": "groq",
            "model": "openai/gpt-oss-20b",
            "rank": 1,
            "reason": "not assigned for standard tier",
        }
    ]


async def test_unavailable_first_choice_falls_to_next_assigned_model(usage, database):
    fleet = Fleet(unavailable={"azure_anthropic"})
    response = await fleet.router(usage, settings()).complete(request(AgentId.FORGE))
    assert served(response) == ASTRA and not fleet.opus.requests and not fleet.groq.requests
    _, events = await records(database)
    payload = events[0].payload
    assert payload["assignment"] == "assigned" and payload["assignment_rank"] == 2
    assert payload["skipped"] == [
        {
            "provider": "azure_anthropic",
            "model": "claude-opus-4-8",
            "rank": 1,
            "reason": "provider unavailable",
        }
    ]


async def test_over_budget_assignment_is_skipped_without_a_call(usage, database):
    config = settings(budgets={"azure_anthropic": {"max_requests_per_day": 0}})
    fleet = Fleet()
    response = await fleet.router(usage, config).complete(request(AgentId.WARDEN))
    assert served(response) == ASTRA and not fleet.opus.requests
    rows, events = await records(database)
    assert len(rows) == len(events) == 1
    (skip,) = events[0].payload["skipped"]
    assert skip["provider"] == "azure_anthropic" and skip["rank"] == 1
    assert skip["reason"].startswith("budget:")


async def test_configured_daily_request_cap_bounds_azure(usage, database):
    config = two_step(budgets={"azure_openai": {"max_requests_per_day": 1}})
    fleet = Fleet()
    router = fleet.router(usage, config)
    assert served(await router.complete(request(AgentId.MUSE))) == ASTRA
    assert served(await router.complete(request(AgentId.MUSE))) == GROQ_20B
    assert len(fleet.astra.requests) == 1
    rows, _ = await records(database)
    assert {(row.provider, row.model) for row in rows} == {ASTRA, GROQ_20B}


@pytest.mark.parametrize("agent", [AgentId.FORGE, AgentId.WARDEN])
async def test_groq_is_not_used_for_opus_agents_when_azure_merely_fails(usage, agent):
    fleet = Fleet(
        responses={
            "azure_anthropic": [ProviderError("azure_anthropic")],
            "azure_openai": [ProviderError("azure_openai")],
        }
    )
    with pytest.raises(NoModelAvailable, match="fallback limit reached"):
        await fleet.router(usage, settings()).complete(request(agent))
    assert len(fleet.opus.requests) == len(fleet.astra.requests) == 1
    assert not fleet.groq.requests


async def test_final_fallback_serves_agent_when_every_assignment_is_unusable(usage, database):
    fleet = Fleet(unavailable={"azure_openai", "azure_anthropic"})
    response = await fleet.router(usage, settings()).complete(request(AgentId.FORGE))
    assert response.provider == "groq"
    _, events = await records(database)
    payload = events[0].payload
    assert payload["assignment"] == "fallback" and payload["assignment_rank"] is None
    assert [skip["rank"] for skip in payload["skipped"]] == [1, 2]


async def test_final_fallback_can_be_disabled(usage, database):
    fleet = Fleet(unavailable={"azure_openai", "azure_anthropic"})
    router = fleet.router(usage, settings(assignment_final_fallback=False))
    with pytest.raises(NoModelAvailable) as raised:
        await router.complete(request(AgentId.FORGE))
    assert "azure_anthropic/claude-opus-4-8: provider unavailable" in raised.value.reasons
    assert not fleet.groq.requests
    assert await records(database) == ([], [])


async def test_open_model_circuit_is_skipped_until_cooldown(usage, database):
    clock = Clock()
    config = two_step(model_circuit_failures=1, model_circuit_cooldown_seconds=60)
    fleet = Fleet(responses={"azure_openai": [ProviderError("azure_openai")]})
    router = fleet.router(usage, config, clock=clock)

    assert served(await router.complete(request(AgentId.MUSE))) == GROQ_20B
    assert router.circuits.open_models() == ["azure_openai/gpt-6-astra"]

    assert served(await router.complete(request(AgentId.MUSE))) == GROQ_20B
    assert len(fleet.astra.requests) == 1

    clock.now += 61
    assert served(await router.complete(request(AgentId.MUSE))) == ASTRA
    assert router.circuits.open_models() == []

    _, events = await records(database)
    skipped = [
        event.payload["skipped"] for event in events if event.payload["status"] == "succeeded"
    ]
    assert skipped[0] == [
        {
            "provider": "azure_openai",
            "model": "gpt-6-astra",
            "rank": 1,
            "reason": "failed: ProviderError",
        }
    ]
    assert any(
        skips
        == [
            {
                "provider": "azure_openai",
                "model": "gpt-6-astra",
                "rank": 1,
                "reason": "circuit open",
            }
        ]
        for skips in skipped
    )


async def test_model_circuit_can_be_disabled(usage):
    fleet = Fleet(
        responses={"azure_openai": [ProviderError("azure_openai"), ProviderError("azure_openai")]}
    )
    router = fleet.router(usage, two_step(model_circuit_failures=0))
    for _ in range(3):
        await router.complete(request(AgentId.MUSE))
    assert len(fleet.astra.requests) == 3
    assert router.circuits.open_models() == []


async def test_pinned_model_bypasses_assignment(usage):
    fleet = Fleet()
    response = await fleet.router(usage, settings()).complete(
        request(AgentId.FORGE, provider="groq", model="openai/gpt-oss-120b")
    )
    assert served(response) == GROQ_120B
    assert not fleet.opus.requests and not fleet.astra.requests


def test_defaults_match_the_agent_policy():
    labels = {
        agent: [entry.label() for entry in entries]
        for agent, entries in default_model_assignments().items()
    }
    assert labels == {
        "ORCHESTR": [
            "azure_openai/gpt-6-astra",
            "azure_anthropic/claude-opus-4-8",
            "groq/openai/gpt-oss-120b",
        ],
        "SCOUT": [
            "azure_openai/gpt-6-astra",
            "azure_anthropic/claude-opus-4-8",
            "groq/openai/gpt-oss-120b",
        ],
        "ROGUE": [
            "azure_anthropic/claude-opus-4-8",
            "azure_openai/gpt-6-astra",
            "groq/qwen/qwen3.8-27b",
        ],
        "FORGE": ["azure_anthropic/claude-opus-4-8", "azure_openai/gpt-6-astra"],
        "WARDEN": ["azure_anthropic/claude-opus-4-8", "azure_openai/gpt-6-astra"],
        "LEDGER": ["azure_openai/gpt-6-astra"],
        "MUSE": [
            "azure_anthropic/claude-opus-4-8",
            "azure_openai/gpt-6-astra",
            "groq/openai/gpt-oss-20b",
        ],
    }


async def test_env_override_replaces_only_the_named_agents(usage, monkeypatch):
    monkeypatch.setenv(
        "AETHER_MODEL_ASSIGNMENT",
        '{"forge": ["groq/qwen/qwen3.8-27b"], "Ledger": '
        '[{"provider": "azure_openai", "model": "gpt-6-astra", "tiers": ["light"]}]}',
    )
    config = ProviderSettings(_env_file=None, max_retries=0)
    assert config.assignments("FORGE") == (
        ModelAssignment(provider="groq", model="qwen/qwen3.8-27b"),
    )
    assert config.assignments("WARDEN") == default_model_assignments()["WARDEN"]
    fleet = Fleet()
    router = fleet.router(usage, config)
    assert served(await router.complete(request(AgentId.FORGE))) == QWEN
    assert served(await router.complete(request(AgentId.WARDEN))) == OPUS
    assert served(await router.complete(request(AgentId.LEDGER, ModelTier.LIGHT))) == ASTRA


async def test_default_key_covers_agents_without_an_assignment(usage, monkeypatch):
    monkeypatch.setenv(
        "AETHER_MODEL_ASSIGNMENT", '{"default": ["azure_anthropic/claude-opus-4-8"], "MUSE": []}'
    )
    config = ProviderSettings(_env_file=None, max_retries=0)
    fleet = Fleet()
    response = await fleet.router(usage, config).complete(request(AgentId.MUSE))
    # An explicit empty list means "no assignment", so MUSE goes straight to the final
    # fallback, which prefers the unlimited Azure deployments over Groq.
    assert response.provider == "azure_openai"
    assert config.assignments("SCOUT") == default_model_assignments()["SCOUT"]


@pytest.mark.parametrize(
    "value",
    [
        '{"NOBODY": ["groq/openai/gpt-oss-20b"]}',
        '{"FORGE": ["no-slash"]}',
        '{"FORGE": [{"provider": "groq", "model": ""}]}',
        '{"FORGE": [{"provider": "groq", "model": "m", "tiers": ["none"]}]}',
        '["azure_openai/gpt-6-astra"]',
    ],
)
def test_invalid_assignment_is_rejected(monkeypatch, value):
    monkeypatch.setenv("AETHER_MODEL_ASSIGNMENT", value)
    with pytest.raises(ValidationError):
        ProviderSettings(_env_file=None)
