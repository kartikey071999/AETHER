"""Per-agent model assignment: the ordered (provider, model) preferences of every agent.

The router walks the requesting agent's list first, skipping entries whose provider is
unavailable, whose model is over budget or whose circuit is open. Only when every assigned
entry is unusable does it fall back to any other eligible model (local first, cheapest first).
Defaults favour the Azure deployments; Groq's small daily quota is kept for the agents that
list it and for the final fallback.

Override with ``AETHER_MODEL_ASSIGNMENT`` (JSON). Each key is an agent id (or ``default`` for
agents without an entry) and replaces that agent's list; other agents keep their defaults::

    {"SCOUT": ["groq/openai/gpt-oss-120b", "azure_openai/gpt-6-astra"],
     "LEDGER": [{"provider": "groq", "model": "openai/gpt-oss-20b", "tiers": ["light"]}]}
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aether.communication.messages import AgentId
from aether.models.contracts import ModelTier

DEFAULT_ASSIGNMENT = "default"


class ModelAssignment(BaseModel):
    """One ranked preference; ``tiers`` restricts it to some request tiers (``None`` = all)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1, max_length=64, pattern=r"^[^/\s]+$")
    model: str = Field(min_length=1, max_length=200)
    tiers: frozenset[ModelTier] | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_key(cls, value: Any) -> Any:
        if isinstance(value, str):
            provider, separator, model = value.strip().partition("/")
            if not separator:
                raise ValueError("model assignments must be provider/model")
            return {"provider": provider, "model": model}
        return value

    @model_validator(mode="after")
    def _check(self) -> "ModelAssignment":
        if self.model != self.model.strip():
            raise ValueError("model names must not have surrounding whitespace")
        if self.tiers is not None and (not self.tiers or ModelTier.NONE in self.tiers):
            raise ValueError("assignment tiers must name at least one LLM tier")
        return self

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def allows(self, tier: ModelTier) -> bool:
        return self.tiers is None or tier in self.tiers

    def label(self) -> str:
        if self.tiers is None:
            return self.key
        tiers = ",".join(tier.value for tier in ModelTier if tier in self.tiers)
        return f"{self.key} [{tiers} only]"


Assignments = dict[str, tuple[ModelAssignment, ...]]

_ASTRA = ModelAssignment(provider="azure_openai", model="gpt-6-astra")
_OPUS = ModelAssignment(provider="azure_anthropic", model="claude-opus-4-8")


def default_model_assignments() -> Assignments:
    return {
        AgentId.ORCHESTR.value: (
            _ASTRA,
            _OPUS,
            ModelAssignment(provider="groq", model="openai/gpt-oss-120b"),
        ),
        AgentId.SCOUT.value: (
            _ASTRA,
            ModelAssignment(provider="groq", model="openai/gpt-oss-120b"),
        ),
        AgentId.ROGUE.value: (
            _OPUS,
            _ASTRA,
            ModelAssignment(provider="groq", model="qwen/qwen3.8-27b"),
        ),
        AgentId.FORGE.value: (_OPUS, _ASTRA),
        AgentId.WARDEN.value: (_OPUS, _ASTRA),
        AgentId.LEDGER.value: (
            ModelAssignment(
                provider="groq", model="openai/gpt-oss-20b", tiers=frozenset({ModelTier.LIGHT})
            ),
            _ASTRA,
        ),
        AgentId.MUSE.value: (_ASTRA, ModelAssignment(provider="groq", model="openai/gpt-oss-20b")),
    }


def assignment_key(name: str) -> str:
    """Canonical key: an ``AgentId`` value or ``default``; anything else is rejected."""
    cleaned = name.strip()
    if cleaned.lower() == DEFAULT_ASSIGNMENT:
        return DEFAULT_ASSIGNMENT
    try:
        return AgentId(cleaned.upper()).value
    except ValueError:
        raise ValueError(f"unknown agent in model assignment: {cleaned[:32]!r}") from None


def merge_assignments(value: Any) -> Any:
    """Overrides replace whole per-agent lists; agents not mentioned keep their defaults."""
    if not isinstance(value, Mapping):
        return value
    merged: dict[str, Any] = dict(default_model_assignments())
    for name, entries in value.items():
        merged[assignment_key(str(name))] = entries
    return merged


def assignments_for(
    assignments: Mapping[str, tuple[ModelAssignment, ...]], agent: str
) -> tuple[ModelAssignment, ...]:
    """The agent's ranked list, else the ``default`` list, else nothing (fallback only)."""
    if agent in assignments:
        return assignments[agent]
    return assignments.get(DEFAULT_ASSIGNMENT, ())
