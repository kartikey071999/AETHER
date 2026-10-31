import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.scout.schemas import ResearchLimits
from aether.communication.messages import AgentId
from aether.config import AetherSettings, normalize_database_url
from aether.models import ProviderSettings
from aether.security import SecuritySettings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


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


def test_agent_runtime_defaults() -> None:
    settings = load()
    assert settings.agent_tick_seconds == 30
    assert settings.agent_tick_timeout_seconds == 120
    assert settings.task_control_poll_seconds == 5
    assert settings.task_interrupt_grace_seconds == 10
    assert settings.orchestr == OrchestrLimits()
    assert settings.research == ResearchLimits()
    assert settings.tick_seconds_for(AgentId.WARDEN) == 30


def test_agent_runtime_env_names_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_AGENT_TICK_SECONDS", "12.5")
    monkeypatch.setenv("AETHER_AGENT_TICK_OVERRIDES", '{"WARDEN": 5}')
    monkeypatch.setenv("AETHER_AGENT_TICK_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("AETHER_TASK_CONTROL_POLL_SECONDS", "0.5")
    monkeypatch.setenv("AETHER_TASK_INTERRUPT_GRACE_SECONDS", "2")
    monkeypatch.setenv("AETHER_ORCHESTR__MAX_ACTIVE_OBJECTIVES", "4")
    monkeypatch.setenv("AETHER_ORCHESTR__PRIORITY__MAX_BOOST_POINTS", "12")
    monkeypatch.setenv("AETHER_RESEARCH", '{"max_sources": 2}')
    settings = load()
    assert settings.agent_tick_seconds == 12.5
    assert settings.tick_seconds_for(AgentId.WARDEN) == 5
    assert settings.tick_seconds_for(AgentId.FORGE) == 12.5
    assert settings.agent_tick_timeout_seconds == 45
    assert settings.task_control_poll_seconds == 0.5
    assert settings.task_interrupt_grace_seconds == 2
    assert settings.orchestr.max_active_objectives == 4
    assert settings.orchestr.priority.max_boost_points == 12
    assert settings.research.max_sources == 2


def test_agent_runtime_settings_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_AGENT_TICK_OVERRIDES", '{"WARDEN": 0}')
    with pytest.raises(ValidationError):
        load()


def _env_names(model: type[BaseModel], prefix: str) -> list[str]:
    names = []
    for name, field in model.model_fields.items():
        alias = field.validation_alias
        full = (alias if isinstance(alias, str) else f"{prefix}{name}").upper()
        nested = field.annotation
        if isinstance(nested, type) and issubclass(nested, BaseModel):
            names.extend(_env_names(nested, f"{full}__"))
        else:
            names.append(full)
    return names


def _documented_names() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def test_every_setting_is_documented_in_env_example() -> None:
    expected = {
        *_env_names(AetherSettings, "AETHER_"),
        *_env_names(SecuritySettings, "AETHER_SECURITY_"),
        *_env_names(ProviderSettings, "AETHER_PROVIDER_"),
    }
    missing = sorted(expected - _documented_names())
    assert not missing, f"settings missing from .env.example: {missing}"


def test_env_example_loads_into_every_settings_class(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _documented_names():
        monkeypatch.delenv(name, raising=False)
    settings = AetherSettings(_env_file=ENV_EXAMPLE)
    assert settings.orchestr == OrchestrLimits()
    assert settings.research == ResearchLimits()
    assert settings.agent_tick_seconds == 30
    assert SecuritySettings(_env_file=ENV_EXAMPLE).max_redirects == 5
    assert ProviderSettings(_env_file=ENV_EXAMPLE).max_retries == 2
