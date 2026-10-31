"""Settings-dependent tests must not depend on a developer's .env file in the working directory."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from aether.agents.scout.schemas import ResearchLimits
from aether.config import AetherSettings, load_settings
from aether.models import ProviderSettings
from aether.security import SecuritySettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Each line is rejected or harmful if a settings class ever reads it.
HOSTILE_ENV_FILE = """\
AETHER_UNKNOWN_KEY=surprise
AETHER_RESEARCH__NOT_A_FIELD=1
AETHER_MAX_CONCURRENT_TASKS=0
AETHER_DATABASE_URL=not-a-database-url://
DATABASE_URL=also-not-a-url
AETHER_PROVIDER_MOCK=0
GROQ_API_KEY=hostile-key
AETHER_SECURITY_ALLOWED_HOSTS=["evil.example"]
"""


@pytest.fixture
def hostile_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env").write_text(HOSTILE_ENV_FILE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_settings_ignore_hostile_env_file(hostile_cwd: Path) -> None:
    settings = AetherSettings()
    assert settings.max_concurrent_tasks == 2
    assert settings.research == ResearchLimits()
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert load_settings().max_concurrent_tasks == 2

    # True comes from the autouse offline_providers fixture; the hostile file's 0 must not apply.
    providers = ProviderSettings()
    assert providers.mock is True
    assert providers.groq_api_key is None

    security = SecuritySettings()
    assert security.allowed_hosts == []


def test_no_settings_variables_leak_into_the_process(hostile_cwd: Path) -> None:
    allowed = {"AETHER_PROVIDER_MOCK", "AETHER_LIVE_TESTS", "AETHER_TEST_POSTGRES_URL"}
    leaked = [name for name in os.environ if name.startswith("AETHER_") and name not in allowed]
    assert leaked == []
    assert "GROQ_API_KEY" not in os.environ


def test_migrations_run_from_a_hostile_directory(
    hostile_cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "hostile.db"
    monkeypatch.setenv("AETHER_DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert "events" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
