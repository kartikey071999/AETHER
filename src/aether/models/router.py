"""Local-first routing with durable budgets and globally bounded completion concurrency."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal

from aether.models.base import (
    LENGTH_FINISH_REASONS,
    EmptyCompletion,
    ModelProvider,
    ProviderError,
    TruncatedCompletion,
    retry_delay,
)
from aether.models.budgets import BudgetExceeded, BudgetGuard, RouteSpend
from aether.models.contracts import CompletionRequest, CompletionResponse, ModelInfo, ModelTier
from aether.models.costs import estimate_cost
from aether.models.settings import ProviderSettings
from aether.security import OperationBlocked

_ORDER = {"ollama": 0, "groq": 1, "mock": 1, "azure_anthropic": 2, "azure_openai": 3}
_PREMIUM_ONLY = frozenset({"azure_anthropic", "azure_openai"})


class NoModelAvailable(RuntimeError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("no eligible model: " + "; ".join(reasons))


class ModelRouter:
    def __init__(
        self,
        providers: Sequence[ModelProvider],
        budgets: BudgetGuard,
        *,
        max_concurrent_llm_requests: int = 2,
        semaphore: asyncio.Semaphore | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrent_llm_requests < 1:
            raise ValueError("LLM concurrency must be positive")
        self.providers = sorted(providers, key=lambda provider: _ORDER.get(provider.name, 1))
        self.budgets = budgets
        self.settings: ProviderSettings = budgets.settings
        self.semaphore = semaphore or asyncio.Semaphore(max_concurrent_llm_requests)
        self.sleeper = sleeper

    async def complete(
        self, request: CompletionRequest, *, spend: RouteSpend | None = None
    ) -> CompletionResponse:
        """Route one completion.

        At most ``settings.max_models_per_request`` distinct models receive a provider call;
        ``spend`` accumulates calls and charged tokens and may carry hard limits.
        """
        if request.tier is ModelTier.NONE:
            raise ValueError("deterministic work must not call an LLM")
        request = request.model_copy(deep=True)
        spend = spend if spend is not None else RouteSpend()
        reasons: list[str] = []
        tried = 0
        capped = False
        limit = self.settings.max_models_per_request
        async with self.semaphore:
            for provider in self.providers:
                if request.provider and request.provider != provider.name:
                    continue
                if provider.name in _PREMIUM_ONLY and request.tier is not ModelTier.PREMIUM:
                    continue
                if tried >= limit or spend.refusal:
                    capped = tried >= limit
                    break
                if not provider.available():
                    reasons.append(f"{provider.name}: unavailable")
                    continue
                try:
                    models = await provider.list_models()
                except (ProviderError, OperationBlocked) as error:
                    reasons.append(f"{provider.name}: {type(error).__name__} during discovery")
                    continue
                eligible = [
                    model
                    for model in models
                    if request.tier in model.tiers
                    and (not request.model or request.model == model.name)
                ]
                eligible.sort(key=lambda model: self._cost(provider.name, model, request))
                for model in eligible:
                    if tried >= limit or spend.refusal:
                        capped = tried >= limit
                        break
                    calls = spend.requests
                    try:
                        routed = self.budgets.prepare_request(provider.name, model, request)
                        return await self._try_model(provider, model, routed, spend)
                    except (BudgetExceeded, ProviderError, OperationBlocked) as error:
                        # Only safe classification is retained; never propagate SDK text into events.
                        detail = (
                            str(error)
                            if isinstance(error, BudgetExceeded)
                            else type(error).__name__
                        )
                        reasons.append(f"{provider.name}/{model.name}: {detail}")
                    finally:
                        # Only a model that actually received a call counts towards the cap.
                        if spend.requests > calls:
                            tried += 1
        if capped:
            reasons.append(f"stopped after {tried} model(s): fallback limit reached")
        raise NoModelAvailable(reasons or ["no provider supports the requested tier/model"])

    def _cost(self, provider: str, model: ModelInfo, request: CompletionRequest) -> Decimal:
        return estimate_cost(
            provider,
            model.name,
            request.input_token_estimate(),
            max(request.max_tokens, model.min_max_tokens),
            prices=self.settings.prices,
        )[0]

    async def _try_model(
        self,
        provider: ModelProvider,
        model: ModelInfo,
        request: CompletionRequest,
        spend: RouteSpend,
    ) -> CompletionResponse:
        attempt = 0
        transient_retries = 0
        length_retried = False
        while True:
            spend.check(request)
            reservation = await self.budgets.reserve(provider.name, request)
            response: CompletionResponse | None = None
            error: BaseException | None = None
            started = time.monotonic()
            try:
                async with asyncio.timeout(self.settings.timeout_seconds):
                    response = await provider.complete(request)
                if response.provider != provider.name or response.model != request.model:
                    raise ProviderError(provider.name, code="response_identity_mismatch")
                if not response.text.strip():
                    raise EmptyCompletion(response)
                if (
                    request.response_format != "text"
                    and response.finish_reason in LENGTH_FINISH_REASONS
                ):
                    # Structured output cut off at the limit cannot be valid JSON.
                    raise TruncatedCompletion(response)
                cost, unknown = estimate_cost(
                    provider.name,
                    request.model,
                    response.input_tokens,
                    response.output_tokens,
                    prices=self.settings.prices,
                )
                response = response.model_copy(
                    update={
                        "cost_usd": float(cost),
                        "cost_estimate_unknown": unknown,
                        "latency_ms": (time.monotonic() - started) * 1000,
                    }
                )
            except TimeoutError:
                error = ProviderError(provider.name, transient=True, code="timeout")
            except (ProviderError, OperationBlocked) as caught:
                error = caught
            except BaseException as caught:
                error = caught
                raise
            finally:
                charged = await self.budgets.finish(
                    reservation,
                    request,
                    response=response if error is None else None,
                    error=error,
                    latency_ms=(time.monotonic() - started) * 1000,
                    attempt=attempt + 1,
                )
                spend.add(*charged)
            attempt += 1
            if error is None:
                if response is None:
                    raise RuntimeError("provider returned no completion")
                return response
            if isinstance(error, EmptyCompletion) and error.truncated and not length_retried:
                # One retry on the same model with a larger output budget; never a loop.
                length_retried = True
                larger = self._length_retry(provider, model, request)
                if larger is not None:
                    request = larger
                    continue
                raise error
            if not (
                isinstance(error, ProviderError)
                and error.transient
                and transient_retries < self.settings.max_retries
            ):
                raise error
            await self.sleeper(retry_delay(self.settings, transient_retries))
            transient_retries += 1

    def _length_retry(
        self, provider: ModelProvider, model: ModelInfo, request: CompletionRequest
    ) -> CompletionRequest | None:
        ceiling = self.settings.length_retry_max_tokens
        if request.max_tokens_cap is not None:
            ceiling = min(ceiling, request.max_tokens_cap)
        larger = min(request.max_tokens * 2, ceiling)
        if larger <= request.max_tokens:
            return None
        try:
            return self.budgets.prepare_request(
                provider.name, model, request.model_copy(update={"max_tokens": larger})
            )
        except BudgetExceeded:
            return None

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()
