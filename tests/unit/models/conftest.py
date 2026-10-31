import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from aether.models import BudgetGuard, ProviderSettings
from aether.observability.recorder import EventRecorder
from aether.security import PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import Base
from aether.storage.repositories.provider_usage import ProviderUsageRepository


@pytest.fixture(autouse=True)
def isolated_provider_env(monkeypatch):
    names = {
        "GROQ_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPEN_AI_KEY",
        "OPENAI_API_VERSION",
        "ANTHROPIC_ENDPOINT",
        "ANTHROPIC_API_KEY",
        "API_VERSION",
        "OLLAMA_BASE_URL",
    }
    names.update(name for name in os.environ if name.startswith("AETHER_PROVIDER_"))
    for name in names:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'models.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


@pytest.fixture
def permissions(database, tmp_path):
    policy = SecurityPolicy(
        workspace_dir=tmp_path / "workspace",
        artifact_dir=tmp_path / "artifacts",
        data_dir=tmp_path / "data",
        allowed_hosts=["api.groq.com", "azure.example.com", "opus.example.com", "localhost"],
    )
    return PermissionService(database.sessions, EventRecorder(), policy)


@pytest.fixture
def settings():
    return ProviderSettings(_env_file=None, max_retries=0)


@pytest.fixture
def usage(database):
    return ProviderUsageRepository(database.sessions)


@pytest.fixture
def guard(usage, settings):
    return BudgetGuard(usage, settings)
