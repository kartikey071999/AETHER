"""Validated runtime configuration loaded from environment variables."""

from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
