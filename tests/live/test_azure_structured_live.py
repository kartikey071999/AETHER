"""One tiny structured call per Azure provider with a real agent schema; opt-in only."""

import os

import pytest

from aether.agents.rogue.schemas import CritiqueDraft
from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetGuard,
    CompletionRequest,
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
@pytest.mark.parametrize("provider_type", [AzureOpenAIProvider, AzureAnthropicProvider])
async def test_one_tiny_structured_azure_completion(tmp_path, provider_type):
    settings = ProviderSettings().model_copy(update={"max_retries": 0})
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
        provider = provider_type(settings, permissions)
        if not provider.available():
            pytest.skip(f"{provider.name} is not configured")
        router = ModelRouter(
            [provider], BudgetGuard(ProviderUsageRepository(database.sessions), settings)
        )
        result = await router.complete(
            CompletionRequest(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You critique candidate approaches. Rank every candidate: "
                            "ranked_indices must list the zero-based indices of ALL candidates, "
                            "best first, with no repeats and never empty. rationale must be a "
                            "non-empty sentence."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Candidates:\n0. Cache results in memory.\n"
                            "1. Recompute on every request.\n"
                            "Which is better for a read-heavy service?"
                        ),
                    },
                ],
                tier=ModelTier.PREMIUM,
                max_tokens=400,
                response_format="json_schema",
                json_schema=CritiqueDraft.model_json_schema(),
                provider=provider.name,
            )
        )
        assert result.provider == provider.name
        CritiqueDraft.model_validate_json(result.text)
    finally:
        if provider is not None:
            await provider.aclose()
        await database.close()
