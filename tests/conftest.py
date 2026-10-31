import os

import pytest
from pydantic_settings import BaseSettings
from sqlalchemy import event
from sqlalchemy.engine import Engine

from aether.config import AetherSettings
from aether.models import ProviderSettings
from aether.security import SecuritySettings

# Unprefixed variables that settings classes read (provider credentials, endpoints, DATABASE_URL).
UNPREFIXED_SETTINGS_ENV = frozenset(
    {
        "DATABASE_URL",
        "GROQ_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPEN_AI_KEY",
        "OPENAI_API_VERSION",
        "ANTHROPIC_ENDPOINT",
        "ANTHROPIC_API_KEY",
        "API_VERSION",
        "OLLAMA_BASE_URL",
    }
)
# Harness controls read by tests themselves; they are not settings and must survive isolation.
HARNESS_ENV = frozenset({"AETHER_LIVE_TESTS", "AETHER_TEST_POSTGRES_URL"})


@event.listens_for(Engine, "connect")
def _skip_sqlite_fsync(dbapi_connection, _record) -> None:
    """Test databases never need to survive power loss: skip the per-commit fsync.

    Locking, transactions and rollback behave exactly as with the default setting; only the
    flush to stable storage is skipped, which costs several milliseconds per commit on Windows.
    """
    if "sqlite" in type(dbapi_connection).__module__:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()


def _settings_classes() -> set[type[BaseSettings]]:
    found: set[type[BaseSettings]] = {AetherSettings, ProviderSettings, SecuritySettings}
    pending = list(BaseSettings.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls not in found:
            found.add(cls)
            pending.extend(cls.__subclasses__())
    return found


@pytest.fixture(autouse=True)
def isolated_settings(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read a developer's .env file or the shell's AETHER_* / provider variables.

    Every settings class stops loading ``.env`` files and the settings-related variables are
    removed from the process environment, so a test's configuration is exactly what it sets.
    Tests marked ``live`` read the real configuration on purpose and are left untouched.
    """
    if request.node.get_closest_marker("live"):
        return
    for settings_class in _settings_classes():
        monkeypatch.setitem(settings_class.model_config, "env_file", None)
    for name in list(os.environ):
        if name in HARNESS_ENV:
            continue
        if name.startswith("AETHER_") or name in UNPREFIXED_SETTINGS_ENV:
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def offline_providers(isolated_settings: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime tests never construct real providers, whatever the developer's .env holds."""
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")
