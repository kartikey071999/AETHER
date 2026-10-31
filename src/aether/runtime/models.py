"""Process-wide model services built once at startup and injected into every agent context."""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aether.agents.base import AgentContext
from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.models import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    BudgetGuard,
    CompletionRequest,
    CompletionResponse,
    GroqProvider,
    MockProvider,
    ModelProvider,
    ModelRouter,
    NoModelAvailable,
    OllamaProvider,
    ProviderSettings,
    provider_hosts,
)
from aether.models.facade import AgentModels, ModelCallContext, ModelUnavailable
from aether.models.governor import ModelGovernor
from aether.observability.recorder import EventRecorder
from aether.security import PermissionService
from aether.storage.repositories.provider_usage import ProviderUsageRepository

OLLAMA = "ollama"


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """One UTC day of recorded usage against the configured daily caps (``None`` = uncapped)."""

    requests: int
    tokens: int
    cost_usd: Decimal
    max_requests: int | None
    max_tokens: int | None
    max_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """Non-secret facts about one provider; never carries endpoints or credentials."""

    name: str
    configured: bool
    available: bool
    models: tuple[str, ...]
    usage: ProviderUsage | None = None


def allowlist_hosts(provider_settings: ProviderSettings) -> frozenset[str]:
    """Hosts to merge into the security allowlist; offline mock runs need none."""
    return frozenset() if provider_settings.mock else provider_hosts(provider_settings)


def build_providers(
    provider_settings: ProviderSettings, permissions: PermissionService
) -> list[ModelProvider]:
    """Every provider, configured or not (constructing one never contacts the network)."""
    if provider_settings.mock:
        return [MockProvider()]
    return [
        OllamaProvider(provider_settings, permissions),
        GroqProvider(provider_settings, permissions),
        AzureAnthropicProvider(provider_settings, permissions),
        AzureOpenAIProvider(provider_settings, permissions),
    ]


@dataclass(slots=True)
class ModelServices:
    """One provider set, one budget guard and one router shared by the whole process."""

    settings: ProviderSettings
    providers: tuple[ModelProvider, ...]
    budgets: BudgetGuard
    router: ModelRouter
    usage: ProviderUsageRepository
    max_concurrent_llm_requests: int
    governor: ModelGovernor
    closed: bool = field(default=False)

    def context_for(self, agent_id: AgentId, base: AgentContext) -> AgentContext:
        """``base`` plus a completion facade bound to ``agent_id``."""
        return replace(
            base,
            models=AgentModels(agent_id, self._complete, report_unusable=self._report_unusable),
        )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        # Agents may not import the router, so its failure is re-raised as a facade type.
        try:
            return await self.governor.complete(
                request, lambda routed, spend: self.router.complete(routed, spend=spend)
            )
        except NoModelAvailable as error:
            raise ModelUnavailable(error.reasons) from None

    async def _report_unusable(self, agent_id: AgentId, ctx: ModelCallContext, reason: str) -> None:
        await self.governor.report_unusable(agent_id, ctx, reason)

    def contexts_for(
        self, agent_ids: Sequence[AgentId], base: AgentContext
    ) -> dict[AgentId, AgentContext]:
        return {agent_id: self.context_for(agent_id, base) for agent_id in agent_ids}

    async def describe(self) -> list[ProviderStatus]:
        """Configuration facts only: no network, no secrets."""
        statuses = []
        for provider in self.providers:
            configured = provider.available()
            if provider.name == OLLAMA:
                # Installed models are only known after discovery; report what is declared.
                names = tuple(sorted(self.settings.ollama_models)) if configured else ()
            else:
                names = tuple(model.name for model in await provider.list_models())
            statuses.append(
                ProviderStatus(
                    name=provider.name,
                    configured=configured,
                    available=configured and bool(names),
                    models=names,
                )
            )
        return statuses

    async def status(self, *, now: datetime | None = None) -> list[ProviderStatus]:
        """``describe`` plus today's recorded usage against each provider's budget."""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        day = current.replace(hour=0, minute=0, second=0, microsecond=0)
        statuses = []
        for status in await self.describe():
            policy = self.settings.budgets.get(status.name)
            async with self.usage.transaction() as session:
                totals = await self.usage.totals_in(
                    session, provider=status.name, since=day, until=day + timedelta(days=1)
                )
            statuses.append(
                replace(
                    status,
                    usage=ProviderUsage(
                        requests=totals.requests,
                        tokens=totals.tokens,
                        cost_usd=totals.cost_usd,
                        max_requests=policy.max_requests_per_day if policy else 0,
                        max_tokens=policy.max_tokens_per_day if policy else 0,
                        max_cost_usd=policy.max_cost_usd_per_day if policy else 0,
                    ),
                )
            )
        return statuses

    async def initialized_payload(self) -> dict[str, Any]:
        """Non-secret facts for ``runtime.models_initialized``: names, availability and counts."""
        statuses = await self.describe()
        return {
            "mock": bool(self.settings.mock),
            "providers": [
                {
                    "name": status.name,
                    "configured": status.configured,
                    "available": status.available,
                    "model_count": len(status.models),
                }
                for status in statuses
            ],
            "model_count": sum(len(status.models) for status in statuses),
            "max_concurrent_llm_requests": self.max_concurrent_llm_requests,
        }

    async def aclose(self) -> None:
        """Close the router and every provider client exactly once."""
        if self.closed:
            return
        self.closed = True
        await self.router.aclose()


def build_model_services(
    provider_settings: ProviderSettings,
    settings: AetherSettings,
    permissions: PermissionService,
    usage: ProviderUsageRepository,
    recorder: EventRecorder,
    *,
    providers: Sequence[ModelProvider] | None = None,
) -> ModelServices:
    """Construct the only budget guard and router of the process."""
    chosen = tuple(
        providers if providers is not None else build_providers(provider_settings, permissions)
    )
    budgets = BudgetGuard(usage, provider_settings, recorder)
    router = ModelRouter(
        chosen,
        budgets,
        max_concurrent_llm_requests=settings.max_concurrent_llm_requests,
    )
    return ModelServices(
        settings=provider_settings,
        providers=chosen,
        budgets=budgets,
        router=router,
        usage=usage,
        max_concurrent_llm_requests=settings.max_concurrent_llm_requests,
        governor=ModelGovernor(provider_settings, sessions=usage.sessions, recorder=recorder),
    )
