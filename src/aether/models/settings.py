"""Provider settings are independent of runtime configuration and never expose secrets."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.models.assignment import (
    ModelAssignment,
    assignments_for,
    default_model_assignments,
    merge_assignments,
)
from aether.models.contracts import ModelTier
from aether.models.costs import ModelPrice


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_requests_per_day: int | None = Field(default=2000, ge=0)
    max_tokens_per_day: int | None = Field(default=2_000_000, ge=0)
    max_cost_usd_per_day: float | None = Field(default=10, ge=0)
    max_tokens_per_request: int = Field(default=32_768, ge=1)
    max_concurrent_requests: int = Field(default=2, ge=1)
    # Unknown prices are admitted only when daily request and token caps bound the spend.
    allow_unpriced: bool = False

    @model_validator(mode="after")
    def _unpriced_needs_caps(self) -> "BudgetPolicy":
        if self.allow_unpriced and (
            self.max_requests_per_day is None or self.max_tokens_per_day is None
        ):
            raise ValueError("allow_unpriced requires max_requests_per_day and max_tokens_per_day")
        return self


def default_budgets() -> dict[str, BudgetPolicy]:
    # Azure prices are not built in, so their spend is bounded by request/token caps; the USD
    # ceilings apply once AETHER_PROVIDER_PRICES declares a price.
    return {
        "groq": BudgetPolicy(),
        "azure_openai": BudgetPolicy(
            max_requests_per_day=1000,
            max_tokens_per_day=3_000_000,
            max_cost_usd_per_day=25,
            max_tokens_per_request=8192,
            max_concurrent_requests=4,
            allow_unpriced=True,
        ),
        "azure_anthropic": BudgetPolicy(
            max_requests_per_day=300,
            max_tokens_per_day=1_500_000,
            max_cost_usd_per_day=50,
            max_tokens_per_request=16_384,
            max_concurrent_requests=2,
            allow_unpriced=True,
        ),
        "ollama": BudgetPolicy(
            max_requests_per_day=None, max_tokens_per_day=None, max_cost_usd_per_day=None
        ),
        "mock": BudgetPolicy(
            max_requests_per_day=None, max_tokens_per_day=None, max_cost_usd_per_day=None
        ),
    }


class AgentBudgetPolicy(BaseModel):
    """Per-objective LLM allowance for one agent (``None`` = unlimited)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_tokens_per_objective: int | None = Field(default=150_000, ge=0)
    max_requests_per_objective: int | None = Field(default=50, ge=0)


DEFAULT_AGENT_BUDGET = "default"


def default_agent_budgets() -> dict[str, AgentBudgetPolicy]:
    """Keys are agent ids (e.g. ``FORGE``); ``default`` applies to every other agent."""
    return {
        DEFAULT_AGENT_BUDGET: AgentBudgetPolicy(),
        "FORGE": AgentBudgetPolicy(max_tokens_per_objective=20_000, max_requests_per_objective=8),
        "ORCHESTR": AgentBudgetPolicy(
            max_tokens_per_objective=40_000, max_requests_per_objective=20
        ),
    }


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AETHER_PROVIDER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
        allow_inf_nan=False,
    )

    groq_api_key: SecretStr | None = Field(default=None, validation_alias="GROQ_API_KEY")
    azure_openai_endpoint: SecretStr | None = Field(
        default=None, validation_alias="AZURE_OPENAI_ENDPOINT"
    )
    azure_openai_key: SecretStr | None = Field(default=None, validation_alias="AZURE_OPEN_AI_KEY")
    openai_api_version: SecretStr | None = Field(
        default=None, validation_alias="OPENAI_API_VERSION"
    )
    anthropic_endpoint: SecretStr | None = Field(
        default=None, validation_alias="ANTHROPIC_ENDPOINT"
    )
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_api_version: SecretStr | None = Field(default=None, validation_alias="API_VERSION")
    ollama_base_url: SecretStr = Field(
        default=SecretStr("http://localhost:11434"), validation_alias="OLLAMA_BASE_URL"
    )
    # AETHER_PROVIDER_MOCK=1 makes the runtime use only the offline MockProvider.
    mock: bool | None = None
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)
    retry_max_delay_seconds: float = Field(default=8, ge=0, le=60)
    budgets: dict[str, BudgetPolicy] = Field(default_factory=default_budgets)
    # Keys are "provider/model"; these caps apply in addition to provider-wide caps.
    model_budgets: dict[str, BudgetPolicy] = Field(default_factory=dict)
    # Per-objective, per-agent LLM allowances; exhaustion forces deterministic fallbacks.
    agent_budgets: dict[str, AgentBudgetPolicy] = Field(default_factory=default_agent_budgets)
    # Distinct models one request may reach before giving up (bounds fallback fan-out).
    max_models_per_request: int = Field(default=2, ge=1, le=10)
    # Ordered (provider, model) preferences per agent; see aether.models.assignment.
    model_assignment: dict[str, tuple[ModelAssignment, ...]] = Field(
        default_factory=default_model_assignments, validation_alias="AETHER_MODEL_ASSIGNMENT"
    )
    # After an agent's assigned models are exhausted, try any other eligible model.
    assignment_final_fallback: bool = True
    # Consecutive failures of one provider/model before the router skips it (0 disables), and
    # how long it is skipped before one more attempt is allowed.
    model_circuit_failures: int = Field(default=3, ge=0)
    model_circuit_cooldown_seconds: float = Field(default=300, ge=0, le=86_400)
    # A length-truncated completion is retried once with doubled max_tokens up to this cap.
    length_retry_max_tokens: int = Field(default=8192, ge=1)
    # Consecutive failures per objective and agent before LLM use is stopped (0 disables).
    circuit_breaker_failures: int = Field(default=5, ge=0)
    # An open breaker allows one half-open probe call after this cool-down (default 30 minutes).
    circuit_breaker_cooldown_seconds: float = Field(default=1800, ge=0, le=604_800)
    prices: dict[str, ModelPrice] = Field(default_factory=dict)
    # Explicit suitability declaration; do not infer reasoning ability from a model's name.
    ollama_models: dict[str, list[ModelTier]] = Field(default_factory=dict)

    @field_validator("prices", "model_budgets")
    @classmethod
    def _validate_model_keys(cls, value: dict) -> dict:
        for key in value:
            provider, separator, model = key.partition("/")
            if (
                not separator
                or not provider.strip()
                or not model.strip()
                or provider != provider.strip()
                or model != model.strip()
                or len(provider) > 64
                or len(model) > 200
            ):
                raise ValueError("model settings keys must be provider/model")
        return value

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_unconfigured(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("azure_openai_endpoint", "anthropic_endpoint", "ollama_base_url")
    @classmethod
    def _validate_endpoint(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        parts = urlsplit(value.get_secret_value())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "provider endpoint must be an HTTP(S) URL without credentials or query"
            )
        if parts.scheme != "https" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote provider endpoints must use HTTPS")
        return value

    @field_validator("budgets", mode="before")
    @classmethod
    def _merge_default_budgets(cls, value: dict) -> dict:
        defaults = default_budgets()
        return {
            **defaults,
            **{
                name: (
                    {**defaults[name].model_dump(), **policy}
                    if name in defaults and isinstance(policy, dict)
                    else policy
                )
                for name, policy in value.items()
            },
        }

    @field_validator("agent_budgets", mode="before")
    @classmethod
    def _merge_default_agent_budgets(cls, value: dict) -> dict:
        defaults = default_agent_budgets()
        return {
            **defaults,
            **{
                name: (
                    {**defaults[name].model_dump(), **policy}
                    if name in defaults and isinstance(policy, dict)
                    else policy
                )
                for name, policy in value.items()
            },
        }

    @field_validator("model_assignment", mode="before")
    @classmethod
    def _merge_default_assignments(cls, value):
        return default_model_assignments() if value is None else merge_assignments(value)

    def assignments(self, agent: str) -> tuple[ModelAssignment, ...]:
        return assignments_for(self.model_assignment, agent)

    def agent_budget(self, agent: str) -> AgentBudgetPolicy:
        return self.agent_budgets.get(agent) or self.agent_budgets.get(
            DEFAULT_AGENT_BUDGET, AgentBudgetPolicy()
        )
