"""Protected model providers; inject one shared router into agent workflows."""

from aether.models.base import ModelProvider, ProviderError
from aether.models.budgets import BudgetExceeded, BudgetGuard, BudgetPolicy
from aether.models.contracts import (
    CompletionMessage,
    CompletionRequest,
    CompletionResponse,
    ModelInfo,
    ModelTier,
    ProviderHealth,
)
from aether.models.hosts import provider_hosts
from aether.models.providers import (
    AzureAnthropicProvider,
    AzureOpenAIProvider,
    GroqProvider,
    MockProvider,
    OllamaProvider,
)
from aether.models.router import ModelRouter, NoModelAvailable
from aether.models.settings import ProviderSettings

__all__ = [
    "AzureAnthropicProvider",
    "AzureOpenAIProvider",
    "BudgetExceeded",
    "BudgetGuard",
    "BudgetPolicy",
    "CompletionMessage",
    "CompletionRequest",
    "CompletionResponse",
    "GroqProvider",
    "MockProvider",
    "ModelInfo",
    "ModelProvider",
    "ModelRouter",
    "ModelTier",
    "NoModelAvailable",
    "OllamaProvider",
    "ProviderError",
    "ProviderHealth",
    "ProviderSettings",
    "provider_hosts",
]
