"""Provider interface and bounded, safe single-attempt execution.

The router owns retries so each attempt is separately budgeted and persisted.
"""

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlsplit

import anthropic
import groq
import openai

from aether.communication.messages import AgentId
from aether.models.contracts import (
    CompletionRequest,
    CompletionResponse,
    ModelInfo,
    ModelTier,
    ProviderHealth,
)
from aether.models.costs import estimate_cost
from aether.models.settings import ProviderSettings
from aether.models.transport import (
    HTTP_ERRORS,
    CallContext,
    call_context,
    is_transient_http_error,
    protect_sdk_logs,
)
from aether.observability.redaction import redact_string
from aether.security import Operation, OperationBlocked, OperationKind, PermissionService


class ProviderError(RuntimeError):
    """Error metadata; ``str()`` never includes provider messages, request bodies, or credentials.

    ``detail`` is a redacted, truncated status/message for durable diagnostics only.
    """

    def __init__(
        self,
        provider: str,
        *,
        transient: bool = False,
        code: str = "failed",
        detail: str | None = None,
    ) -> None:
        self.provider = provider
        self.transient = transient
        self.code = code
        self.detail = detail
        super().__init__(f"{provider}: {code}")


ERROR_DETAIL_LIMIT = 300


def error_detail(error: BaseException, status: object) -> str:
    """Status code and provider message, secret-redacted first and then truncated."""
    message = " ".join(str(getattr(error, "message", None) or error).split())
    label = f"HTTP {status}" if status else type(error).__name__
    return redact_string(f"{label}: {message}" if message else label)[:ERROR_DETAIL_LIMIT]


LENGTH_FINISH_REASONS = frozenset({"length", "max_tokens"})


class EmptyCompletion(ProviderError):
    """An unusable completion whose measured (or estimated) usage still counts."""

    code_name = "empty_completion"

    def __init__(self, response: CompletionResponse) -> None:
        self.finish_reason = response.finish_reason
        self.input_tokens = response.input_tokens
        self.output_tokens = response.output_tokens
        self.usage_estimated = response.usage_estimated
        super().__init__(response.provider, code=self.code_name)

    @property
    def truncated(self) -> bool:
        """The model stopped at its output limit, so a larger budget may succeed."""
        return self.finish_reason in LENGTH_FINISH_REASONS


class TruncatedCompletion(EmptyCompletion):
    """Structured output cut off at the token limit: it cannot be valid, so it is unusable."""

    code_name = "truncated_completion"


class ModelProvider(Protocol):
    name: str

    def available(self) -> bool: ...
    async def list_models(self) -> list[ModelInfo]: ...
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...
    async def health(self) -> ProviderHealth: ...
    async def aclose(self) -> None: ...


class RemoteProvider:
    name: str
    endpoint: str

    def __init__(self, settings: ProviderSettings, permissions: PermissionService) -> None:
        protect_sdk_logs()
        self.settings = settings
        self.permissions = permissions

    async def _run[T](
        self, call: Callable[[], Awaitable[T]], request: CompletionRequest | None = None
    ) -> T:
        if not self.available():
            raise ProviderError(self.name, code="unavailable")
        context = CallContext(
            agent=request.agent if request else AgentId.ORCHESTR,
            task_id=request.task_id if request else None,
            objective_id=request.objective_id if request else None,
        )
        operation = Operation(
            kind=OperationKind.LLM_CALL,
            target=self.endpoint,
            hosts=(urlsplit(self.endpoint).hostname or "",),
            arguments={
                "provider": self.name,
                "request_sha256": (
                    hashlib.sha256(request.model_dump_json().encode()).hexdigest()
                    if request
                    else "discovery"
                ),
            },
            requested_by=context.agent,
            task_id=context.task_id,
            objective_id=context.objective_id,
        )
        grant = await self.permissions.authorize(operation)
        await self.permissions.redeem(grant, operation)
        try:
            with call_context(context):
                async with asyncio.timeout(
                    min(self.settings.timeout_seconds, self.permissions.policy.http_timeout_seconds)
                ):
                    return await call()
        except (
            groq.APIError,
            openai.APIError,
            anthropic.APIError,
            TimeoutError,
            *HTTP_ERRORS,
        ) as error:
            # SDKs wrap exceptions from HTTP hooks. Preserve the durable security block.
            cause: BaseException | None = error
            while cause is not None:
                if isinstance(cause, OperationBlocked):
                    raise cause from None
                cause = cause.__cause__
            status = getattr(error, "status_code", None)
            transient = (
                isinstance(
                    error,
                    TimeoutError
                    | groq.APIConnectionError
                    | openai.APIConnectionError
                    | anthropic.APIConnectionError,
                )
                or status == 429
                or (isinstance(status, int) and status >= 500)
                or is_transient_http_error(error)
            )
            raise ProviderError(
                self.name,
                transient=transient,
                code=f"http_{status}" if status else "transport_error",
                detail=error_detail(error, status),
            ) from None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.tier is ModelTier.NONE:
            raise ValueError("deterministic work must not call an LLM")
        if request.model is None:
            raise ValueError("a routed model is required")
        models = await self.list_models()
        model = next(
            (
                model
                for model in models
                if model.name == request.model and request.tier in model.tiers
            ),
            None,
        )
        if model is None:
            raise ProviderError(self.name, code="unsupported_model_or_tier")
        try:
            request = request.with_model_limits(model)
        except ValueError as error:
            raise ProviderError(self.name, code=str(error)) from None
        started = time.monotonic()
        response = await self._run(lambda: self._complete(request), request)
        if not response.text.strip():
            raise EmptyCompletion(response)
        cost, unknown = estimate_cost(
            self.name,
            model.name,
            response.input_tokens,
            response.output_tokens,
            prices=self.settings.prices,
        )
        return response.model_copy(
            update={
                "latency_ms": (time.monotonic() - started) * 1000,
                "cost_usd": float(cost),
                "cost_estimate_unknown": unknown,
            }
        )

    async def _complete(self, request: CompletionRequest) -> CompletionResponse:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError

    async def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    async def health(self) -> ProviderHealth:
        if not self.available():
            return ProviderHealth(available=False, healthy=False, detail="not configured")
        try:
            await self._run(self._probe)
        except (ProviderError, OperationBlocked) as error:
            return ProviderHealth(available=True, healthy=False, detail=type(error).__name__)
        return ProviderHealth(available=True, healthy=True, detail="reachable")

    async def _probe(self) -> object:
        raise NotImplementedError


def retry_delay(settings: ProviderSettings, attempt: int) -> float:
    return min(settings.retry_backoff_seconds * 2**attempt, settings.retry_max_delay_seconds)
