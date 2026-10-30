"""Validated runtime configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AetherSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AETHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr = SecretStr("postgresql+asyncpg://aether:aether@localhost:5432/aether")
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    artifact_dir: Path = Path("artifacts")
    max_concurrent_tasks: int = Field(default=2, ge=1, le=64)
    max_concurrent_builds: int = Field(default=1, ge=1, le=16)
    max_concurrent_llm_requests: int = Field(default=2, ge=1, le=64)
    min_free_disk_gb: float = Field(default=5.0, ge=0)
    task_timeout_seconds: int = Field(default=1800, ge=1)
    max_task_retries: int = Field(default=3, ge=0, le=20)


def load_settings() -> AetherSettings:
    return AetherSettings()
