"""Per-agent model assignment with durable budgets and globally bounded concurrency."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
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


class NoModelAvailable(RuntimeError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("no eligible model: " + "; ".join(reasons))


@dataclass(frozen=True, slots=True)
class _Candidate:
    provider: ModelProvider
    model: ModelInfo
    # 1-based position in the agent's assignment; ``None`` for the final fallback.
    rank: int | None

    @property
    def key(self) -> str:
        return f"{self.provider.name}/{self.model.name}"


@dataclass(slots=True)
class _Walk:
    """What one routed completion skipped, for audit events and the final error."""

    skipped: list[dict[str, object]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    catalog: dict[str, list[ModelInfo] | None] = field(default_factory=dict)

    def skip(self, provider: str, model: str, rank: int | None, reason: str) -> None:
        self.skipped.append({"provider": provider, "model": model, "rank": rank, "reason": reason})
        self.reasons.append(f"{provider}/{model}: {reason}")


class ModelCircuits:
    """Process-wide consecutive-failure breaker per provider/model, shared by every agent.

    After ``model_circuit_failures`` consecutive failed calls the model is skipped for
    ``model_circuit_cooldown_seconds``; then one more attempt is allowed, and a failure re-opens
    it while a success closes it.
    """

    def __init__(
        self, settings: ProviderSettings, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.settings = settings
        self.clock = clock
        self._failures: dict[str, int] = {}
        self._opened: dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        opened = self._opened.get(key)
        return (
            opened is not None
            and self.clock() - opened < self.settings.model_circuit_cooldown_seconds
        )

    def failed(self, key: str) -> None:
        threshold = self.settings.model_circuit_failures
        if not threshold:
            return
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        if failures >= threshold:
            self._opened[key] = self.clock()

    def succeeded(self, key: str) -> None:
        self._failures.pop(key, None)
        self._opened.pop(key, None)

    def open_models(self) -> list[str]:
        return sorted(key for key in self._opened if self.is_open(key))


class ModelRouter:
    def __init__(
        self,
        providers: Sequence[ModelProvider],
        budgets: BudgetGuard,
        *,
        max_concurrent_llm_requests: int = 2,
        semaphore: asyncio.Semaphore | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrent_llm_requests < 1:
            raise ValueError("LLM concurrency must be positive")
        self.providers = sorted(providers, key=lambda provider: _ORDER.get(provider.name, 1))
        self.budgets = budgets
        self.settings: ProviderSettings = budgets.settings
        self.semaphore = semaphore or asyncio.Semaphore(max_concurrent_llm_requests)
        self.sleeper = sleeper
        self.circuits = ModelCircuits(self.settings, clock)

    async def complete(
        self, request: CompletionRequest, *, spend: RouteSpend | None = None
    ) -> CompletionResponse:
        """Route one completion along the requesting agent's model assignment.

        Assigned models are tried in rank order; unavailable, over-budget or circuit-open
        entries are skipped. When all are unusable, any other eligible model is the final
        fallback. At most ``settings.max_models_per_request`` distinct models receive a provider
        call; ``spend`` accumulates calls and charged tokens and may carry hard limits.
        """
        if request.tier is ModelTier.NONE:
            raise ValueError("deterministic work must not call an LLM")
        request = request.model_copy(deep=True)
        spend = spend if spend is not None else RouteSpend()
        walk = _Walk()
        tried = 0
        capped = False
        limit = self.settings.max_models_per_request
        async with self.semaphore, aclosing(self._candidates(request, walk)) as candidates:
            async for candidate in candidates:
                if tried >= limit or spend.refusal:
                    capped = tried >= limit
                    break
                provider, model = candidate.provider, candidate.model
                calls = spend.requests
                routing = {
                    "assignment_rank": candidate.rank,
                    "assignment": "assigned" if candidate.rank is not None else "fallback",
                    "skipped": list(walk.skipped),
                }
                try:
                    routed = self.budgets.prepare_request(provider.name, model, request)
                    response = await self._try_model(provider, model, routed, spend, routing)
                except BudgetExceeded as error:
                    walk.skip(provider.name, model.name, candidate.rank, f"budget: {error}")
                except (ProviderError, OperationBlocked) as error:
                    if isinstance(error, ProviderError):
                        self.circuits.failed(candidate.key)
                    # Only safe classification is retained; never propagate SDK text into events.
                    walk.skip(
                        provider.name, model.name, candidate.rank, f"failed: {type(error).__name__}"
                    )
                else:
                    self.circuits.succeeded(candidate.key)
                    return response
                finally:
                    # Only a model that actually received a call counts towards the cap.
                    if spend.requests > calls:
                        tried += 1
        if capped:
            walk.reasons.append(f"stopped after {tried} model(s): fallback limit reached")
        raise NoModelAvailable(walk.reasons or ["no provider supports the requested tier/model"])

    async def _models(self, provider: ModelProvider, walk: _Walk) -> list[ModelInfo] | None:
        """The provider's catalog, discovered at most once per routed completion."""
        if provider.name in walk.catalog:
            return walk.catalog[provider.name]
        models: list[ModelInfo] | None = None
        if not provider.available():
            walk.reasons.append(f"{provider.name}: unavailable")
        else:
            try:
                models = await provider.list_models()
            except (ProviderError, OperationBlocked) as error:
                walk.reasons.append(f"{provider.name}: {type(error).__name__} during discovery")
        walk.catalog[provider.name] = models
        return models

    async def _candidates(
        self, request: CompletionRequest, walk: _Walk
    ) -> AsyncIterator[_Candidate]:
        pinned = request.provider is not None or request.model is not None

        def wanted(provider: str, model: str | None = None) -> bool:
            return (request.provider is None or request.provider == provider) and (
                model is None or request.model is None or request.model == model
            )

        by_name: dict[str, ModelProvider] = {}
        for provider in self.providers:
            by_name.setdefault(provider.name, provider)
        assigned = self.settings.assignments(request.agent.value)
        listed = {entry.key for entry in assigned}
        seen: set[str] = set()
        for rank, entry in enumerate(assigned, start=1):
            provider = by_name.get(entry.provider)
            # Providers this process does not run at all (e.g. mock mode) are not candidates.
            if provider is None or entry.key in seen or not wanted(entry.provider, entry.model):
                continue
            seen.add(entry.key)
            model: ModelInfo | None = None
            if not entry.allows(request.tier):
                reason = f"not assigned for {request.tier.value} tier"
            elif not provider.available():
                reason = "provider unavailable"
            elif (models := await self._models(provider, walk)) is None:
                reason = "model discovery failed"
            elif (model := next((m for m in models if m.name == entry.model), None)) is None:
                reason = "model not offered"
            elif request.tier not in model.tiers:
                reason = f"{request.tier.value} tier unsupported"
            elif self.circuits.is_open(entry.key):
                reason = "circuit open"
            else:
                yield _Candidate(provider, model, rank)
                continue
            walk.skip(entry.provider, entry.model, rank, reason)
        if not (self.settings.assignment_final_fallback or pinned):
            return
        for provider in self.providers:
            if not wanted(provider.name):
                continue
            models = await self._models(provider, walk)
            eligible = [
                model
                for model in models or []
                if request.tier in model.tiers
                and wanted(provider.name, model.name)
                and f"{provider.name}/{model.name}" not in listed
            ]
            eligible.sort(key=lambda model: self._cost(provider.name, model, request))
            for model in eligible:
                candidate = _Candidate(provider, model, None)
                if self.circuits.is_open(candidate.key):
                    walk.skip(provider.name, model.name, None, "circuit open")
                    continue
                yield candidate

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
        routing: dict[str, object] | None = None,
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
                    raise EmptyCompletion(response, max_tokens=request.max_tokens)
                if (
                    request.response_format != "text"
                    and response.finish_reason in LENGTH_FINISH_REASONS
                ):
                    # Structured output cut off at the limit cannot be valid JSON.
                    raise TruncatedCompletion(response, max_tokens=request.max_tokens)
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
                    routing=routing,
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
