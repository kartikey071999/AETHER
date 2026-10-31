"""Provider settings are independent of runtime configuration and never expose secrets."""

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.models.contracts import ModelTier


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_requests_per_day: int | None = Field(default=2000, ge=0)
    max_tokens_per_day: int | None = Field(default=2_000_000, ge=0)
    max_cost_usd_per_day: float | None = Field(default=10, ge=0)
    max_tokens_per_request: int = Field(default=32_768, ge=1)
    max_concurrent_requests: int = Field(default=2, ge=1)


def default_budgets() -> dict[str, BudgetPolicy]:
    return {
        "groq": BudgetPolicy(),
        "azure_openai": BudgetPolicy(
            max_requests_per_day=50,
            max_tokens_per_day=100_000,
            max_cost_usd_per_day=1,
            max_tokens_per_request=8192,
            max_concurrent_requests=1,
        ),
        "azure_anthropic": BudgetPolicy(
            max_requests_per_day=50,
            max_tokens_per_day=200_000,
            max_cost_usd_per_day=5,
            max_tokens_per_request=16_384,
            max_concurrent_requests=1,
        ),
        "ollama": BudgetPolicy(
            max_requests_per_day=None, max_tokens_per_day=None, max_cost_usd_per_day=None
        ),
        "mock": BudgetPolicy(
            max_requests_per_day=None, max_tokens_per_day=None, max_cost_usd_per_day=None
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
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)
    retry_max_delay_seconds: float = Field(default=8, ge=0, le=60)
    budgets: dict[str, BudgetPolicy] = Field(default_factory=default_budgets)
    # Keys are "provider/model"; these caps apply in addition to provider-wide caps.
    model_budgets: dict[str, BudgetPolicy] = Field(default_factory=dict)
    # Explicit suitability declaration; do not infer reasoning ability from a model's name.
    ollama_models: dict[str, list[ModelTier]] = Field(default_factory=dict)

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
