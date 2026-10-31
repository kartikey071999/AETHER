"""One tiny opt-in call; deliberately outside the isolated offline test fixtures."""

import os

import pytest

from aether.models import (
    BudgetGuard,
    CompletionRequest,
    GroqProvider,
    ModelRouter,
    ModelTier,
    ProviderSettings,
    provider_hosts,
)
from aether.observability.recorder import EventRecorder
from aether.security import PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import Base
from aether.storage.repositories.provider_usage import ProviderUsageRepository


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("AETHER_LIVE_TESTS") != "1", reason="live calls are explicitly opt-in"
)
async def test_one_tiny_groq_completion(tmp_path):
    settings = ProviderSettings().model_copy(update={"max_retries": 0})
    if settings.groq_api_key is None:
        pytest.skip("Groq is not configured")
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'live-usage.db').as_posix()}")
    provider = None
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        permissions = PermissionService(
            database.sessions,
            EventRecorder(),
            SecurityPolicy(
                workspace_dir=tmp_path,
                artifact_dir=tmp_path,
                data_dir=tmp_path,
                allowed_hosts=provider_hosts(settings),
            ),
        )
        provider = GroqProvider(settings, permissions)
        router = ModelRouter(
            [provider],
            BudgetGuard(ProviderUsageRepository(database.sessions), settings),
        )
        result = await router.complete(
            CompletionRequest(
                messages=[{"role": "user", "content": "Say OK."}],
                tier=ModelTier.LIGHT,
                max_tokens=200,  # gpt-oss spends early tokens on reasoning
            )
        )
        assert result.provider == "groq" and result.output_tokens <= 200
        assert result.text.strip()
    finally:
        if provider is not None:
            await provider.aclose()
        await database.close()
