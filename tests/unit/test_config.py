from pathlib import Path

import pytest
from pydantic import ValidationError

from aether.config import AetherSettings, normalize_database_url


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AETHER_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def load() -> AetherSettings:
    return AetherSettings(_env_file=None)


def test_default_database_url_uses_asyncpg() -> None:
    assert load().database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_plain_database_url_is_used_as_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@dbhost:5432/postgres")
    settings = load()
    expected = "postgresql+asyncpg://user:pw@dbhost:5432/postgres"
    assert settings.database_url.get_secret_value() == expected
    assert settings.database_url_for_alembic == settings.database_url.get_secret_value()


def test_aether_database_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@fallback:5432/other")
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgresql://user:pw@primary:5432/aether")
    assert "primary" in load().database_url.get_secret_value()


def test_blank_aether_url_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@fallback:5432/other")
    assert "fallback" in load().database_url.get_secret_value()


def test_env_file_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://u:p@fallback/db\n"
        "AETHER_DATABASE_URL=postgres://u:p@primary/db\n",
        encoding="utf-8",
    )
    settings = AetherSettings(_env_file=env_file)
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@primary/db"

    env_file.write_text("DATABASE_URL=postgresql://u:p@fallback/db\n", encoding="utf-8")
    settings = AetherSettings(_env_file=env_file)
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@fallback/db"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@h:5432/d",
        "postgres://u:p@h:5432/d",
        "postgresql+psycopg://u:p@h:5432/d",
        "postgresql+asyncpg://u:p@h:5432/d",
    ],
)
def test_postgres_urls_are_normalised(url: str) -> None:
    assert normalize_database_url(url) == "postgresql+asyncpg://u:p@h:5432/d"


def test_sqlite_url_is_left_alone() -> None:
    url = "sqlite+aiosqlite:///:memory:"
    assert normalize_database_url(url) == url


def test_normalisation_preserves_special_password_and_maps_sslmode() -> None:
    result = normalize_database_url("postgresql://u:p%40ss%25@h/d?sslmode=require")
    assert result == "postgresql+asyncpg://u:p%40ss%25@h/d?ssl=require"


def test_database_url_stays_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:topsecret@h/d")
    settings = load()
    assert "topsecret" not in repr(settings)
    assert "topsecret" not in str(settings.model_dump())


def test_new_settings_defaults() -> None:
    settings = load()
    assert settings.workspace_dir == Path("workspaces")
    assert settings.log_retention_days == 30
    assert settings.log_max_bytes == 10 * 1024 * 1024
    assert settings.log_backup_count == 5


def test_settings_overrides_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_WORKSPACE_DIR", "C:\\aether\\work")
    monkeypatch.setenv("AETHER_LOG_RETENTION_DAYS", "7")
    settings = load()
    assert settings.workspace_dir == Path("C:\\aether\\work")
    assert settings.log_retention_days == 7

    monkeypatch.setenv("AETHER_LOG_RETENTION_DAYS", "0")
    with pytest.raises(ValidationError):
        load()
