"""Audited HTTP boundary for provider SDKs and local Ollama.

Every actual HTTP request consumes its own grant, including SDK discovery calls.
Redirects, environment proxies, and implicit SDK retries are disabled.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

import httpx
import httpx2

from aether.communication.messages import AgentId
from aether.security import Operation, OperationKind, PermissionService


@dataclass(frozen=True)
class CallContext:
    agent: AgentId
    task_id: UUID | None = None
    objective_id: UUID | None = None


_CONTEXT: ContextVar[CallContext | None] = ContextVar("provider_call_context", default=None)


class _MetadataOnly(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # SDK DEBUG diagnostics include raw request bodies and headers.
        return record.levelno > logging.DEBUG


_SDK_FILTER = _MetadataOnly()
HttpTransport = httpx.AsyncBaseTransport
HttpResponse = httpx.Response


def protect_sdk_logs() -> None:
    for name in ("groq._base_client", "openai._base_client", "anthropic._base_client"):
        logger = logging.getLogger(name)
        if _SDK_FILTER not in logger.filters:
            logger.addFilter(_SDK_FILTER)


@contextmanager
def call_context(context: CallContext) -> Iterator[None]:
    token = _CONTEXT.set(context)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _authorization_hook(
    permissions: PermissionService,
) -> Callable[[httpx.Request | httpx2.Request], Awaitable[None]]:
    async def authorize(request: httpx.Request | httpx2.Request) -> None:
        context = _CONTEXT.get()
        if context is None:
            raise RuntimeError("provider HTTP request has no authorized call context")
        operation = Operation(
            kind=OperationKind.LLM_CALL,
            target=str(request.url),
            hosts=(request.url.host,),
            arguments={
                "method": request.method,
                "body_sha256": hashlib.sha256(await request.aread()).hexdigest(),
                "headers_sha256": hashlib.sha256(
                    repr(sorted(request.headers.multi_items())).encode()
                ).hexdigest(),
            },
            requested_by=context.agent,
            task_id=context.task_id,
            objective_id=context.objective_id,
        )
        grant = await permissions.authorize(operation)
        await permissions.redeem(grant, operation)

    return authorize


def create_client(
    permissions: PermissionService,
    timeout: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    protect_sdk_logs()
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [_authorization_hook(permissions)]},
    )


def create_anthropic_client(
    permissions: PermissionService,
    timeout: float,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """Anthropic 2.x uses its existing httpx2 dependency, not httpx clients."""
    protect_sdk_logs()
    return httpx2.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        event_hooks={"request": [_authorization_hook(permissions)]},
    )


HTTP_ERRORS = (httpx.HTTPError, httpx2.HTTPError)


def is_transient_http_error(error: Exception) -> bool:
    if isinstance(
        error,
        httpx.TimeoutException
        | httpx.TransportError
        | httpx2.TimeoutException
        | httpx2.TransportError,
    ):
        return True
    return isinstance(error, httpx.HTTPStatusError | httpx2.HTTPStatusError) and (
        error.response.status_code == 429 or error.response.status_code >= 500
    )
