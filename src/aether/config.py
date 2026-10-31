"""Validated runtime configuration loaded from environment variables."""

from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.scout.schemas import ResearchLimits
from aether.communication.messages import AgentId

ASYNC_POSTGRES_DRIVER = "postgresql+asyncpg"
_POSTGRES_DRIVERS = {
    "postgres",
    "postgresql",
    "postgresql+psycopg",
    "postgresql+psycopg2",
    "postgresql+asyncpg",
}
# libpq query parameters mapped to their asyncpg equivalents; None drops the parameter.
_QUERY_PARAMETER_MAP: dict[str, str | None] = {"sslmode": "ssl", "channel_binding": None}


def normalize_database_url(url: str) -> str:
    """Return ``url`` using the asyncpg driver for any PostgreSQL URL style."""
    url = url.strip()
    scheme, separator, rest = url.partition("://")
    if not separator or scheme.lower() not in _POSTGRES_DRIVERS:
        return url
    # Only the scheme and query are rewritten so the credentials keep their original encoding.
    location, _, query = rest.partition("?")
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        mapped = _QUERY_PARAMETER_MAP.get(key, key)
        if mapped is not None:
            pairs.append((mapped, value))
    result = f"{ASYNC_POSTGRES_DRIVER}://{location}"
    return f"{result}?{urlencode(pairs)}" if pairs else result


_DEFAULT_DATABASE_URL = "postgresql+asyncpg://aether:aether@localhost:5432/aether"


class AetherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AETHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # AETHER_DATABASE_URL wins; plain DATABASE_URL is the fallback (see _resolve_database_url).
    database_url: SecretStr = SecretStr(_DEFAULT_DATABASE_URL)
    fallback_database_url: SecretStr | None = Field(
        default=None,
        validation_alias="DATABASE_URL",
        exclude=True,
        repr=False,
    )
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    artifact_dir: Path = Path("artifacts")
    workspace_dir: Path = Path("workspaces")
    log_retention_days: int = Field(default=30, ge=1)
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    log_backup_count: int = Field(default=5, ge=0)
    max_concurrent_tasks: int = Field(default=2, ge=1, le=64)
    max_concurrent_builds: int = Field(default=1, ge=1, le=16)
    max_concurrent_llm_requests: int = Field(default=2, ge=1, le=64)
    min_free_disk_gb: float = Field(default=5.0, ge=0)
    task_timeout_seconds: int = Field(default=1800, ge=1)
    max_task_retries: int = Field(default=3, ge=0, le=20)
    api_token: SecretStr | None = Field(default=None, repr=False)
    api_host: str = Field(default="127.0.0.1", min_length=1)
    api_port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("api_token", mode="before")
    @classmethod
    def _empty_api_token(cls, value: SecretStr | str | None) -> SecretStr | str | None:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return None if raw is not None and not raw.strip() else value

    resource_sample_seconds: float = Field(default=30.0, ge=1, allow_inf_nan=False)
    resource_retention_days: int = Field(default=30, ge=1)
    resource_event_seconds: float = Field(default=300.0, ge=60, allow_inf_nan=False)

    # Agent runtime hooks. Nested limits accept JSON (AETHER_ORCHESTR='{"max_active_objectives": 5}')
    # or __-delimited keys (AETHER_ORCHESTR__MAX_ACTIVE_OBJECTIVES=5); tick overrides are a JSON
    # object keyed by agent id (AETHER_AGENT_TICK_OVERRIDES='{"WARDEN": 5}').
    agent_tick_seconds: float = Field(default=30.0, gt=0)
    agent_tick_overrides: dict[AgentId, float] = Field(default_factory=dict)
    agent_tick_timeout_seconds: float = Field(default=120.0, gt=0)
    task_control_poll_seconds: float = Field(default=5.0, gt=0)
    task_interrupt_grace_seconds: float = Field(default=10.0, ge=0)
    orchestr: OrchestrLimits = Field(default_factory=OrchestrLimits)
    research: ResearchLimits = Field(default_factory=ResearchLimits)
    # MUSE suppresses a notice repeating one the same audience got for the same objective and
    # notice kind within this many minutes (0 disables duplicate suppression).
    muse_dedupe_window_minutes: float = Field(default=60.0, ge=0, allow_inf_nan=False)

    @field_validator("agent_tick_overrides")
    @classmethod
    def _positive_intervals(cls, value: dict[AgentId, float]) -> dict[AgentId, float]:
        for agent_id, seconds in value.items():
            if not seconds > 0:
                raise ValueError(f"tick interval for {agent_id.value} must be positive")
        return value

    def tick_seconds_for(self, agent_id: AgentId) -> float:
        return self.agent_tick_overrides.get(agent_id, self.agent_tick_seconds)

    @model_validator(mode="after")
    def _resolve_database_url(self) -> "AetherSettings":
        primary = self.database_url.get_secret_value().strip()
        fallback = (
            self.fallback_database_url.get_secret_value().strip()
            if self.fallback_database_url is not None
            else ""
        )
        explicit = "database_url" in self.model_fields_set and primary
        chosen = primary if explicit or not fallback else fallback
        self.database_url = SecretStr(normalize_database_url(chosen or _DEFAULT_DATABASE_URL))
        return self

    @property
    def database_url_for_alembic(self) -> str:
        # migrations/env.py runs an async engine, so Alembic uses the asyncpg URL as-is.
        return self.database_url.get_secret_value()


def load_settings() -> AetherSettings:
    return AetherSettings()
