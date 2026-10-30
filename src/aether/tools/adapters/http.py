"""SafeHttp: allowlisted HTTP(S) requests whose responses are returned as untrusted content."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import EventRecorder
from aether.security._audit import stage_event
from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import SecurityPolicy, url_host
from aether.security.untrusted import UntrustedContent
from aether.tools.adapters._base import ExecutionFailed, admit
from aether.tools.registry import OperationSpec

SOURCE = "aether.tools.http"
_SCHEMES = frozenset({"http", "https"})
_CROSS_HOST_DROPPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_REDIRECTABLE_METHODS = frozenset({"GET", "HEAD"})


class HttpInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)


@dataclass(frozen=True, slots=True)
class HttpResult:
    status_code: int
    url: str
    headers: dict[str, str]
    body: UntrustedContent
    truncated: bool
    redirects: tuple[str, ...]


def _check_url(url: str, policy: SecurityPolicy) -> str | None:
    try:
        scheme = httpx.URL(url).scheme
    except httpx.InvalidURL:
        return "URL is invalid"
    if scheme not in _SCHEMES:
        return f"scheme {scheme!r} is not allowed"
    host = url_host(url)
    if host is None or not policy.is_host_allowed(host):
        return f"host {host!r} is not allowlisted"
    return None


class SafeHttp:
    kinds = frozenset({OperationKind.HTTP_REQUEST})

    def __init__(
        self,
        permissions: PermissionService,
        sessions: async_sessionmaker[AsyncSession],
        recorder: EventRecorder,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._permissions = permissions
        self._sessions = sessions
        self._recorder = recorder
        self._transport = transport
        self._clock = clock

    @property
    def _policy(self) -> SecurityPolicy:
        return self._permissions.policy

    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec:
        if not isinstance(payload, HttpInput):
            raise TypeError("SafeHttp expects HttpInput")
        host = url_host(payload.url)
        if host is None or httpx.URL(payload.url).scheme not in _SCHEMES:
            raise ValueError("url must be an absolute http(s) URL")
        limit = self._policy.http_timeout_seconds
        return OperationSpec(
            target=payload.url,
            arguments={
                "method": payload.method,
                "url": payload.url,
                "headers": dict(payload.headers),
                "body": payload.body,
                "timeout_seconds": min(payload.timeout_seconds or limit, limit),
            },
            hosts=(host,),
        )

    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> HttpResult:
        operation, grant = await admit(
            self._permissions, self.kinds, operation, authorization, correlation_id=correlation_id
        )
        policy = self._policy
        arguments = operation.arguments
        try:
            payload = HttpInput.model_validate(arguments)
        except ValueError as error:
            raise await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                f"malformed request arguments: {type(error).__name__}",
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            ) from error
        if payload.url != operation.target:
            raise await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                "request URL does not match the authorized target",
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            )
        timeout = min(
            payload.timeout_seconds or policy.http_timeout_seconds, policy.http_timeout_seconds
        )
        execution_id = uuid4()
        await self._record(
            operation,
            "execution.started",
            {"execution_id": execution_id, "operation": operation.describe()},
            execution_id,
            correlation_id,
        )
        started = time.monotonic()
        try:
            result = await self._send(operation, grant, payload, timeout, correlation_id)
        except Exception as error:
            await self._finish(operation, execution_id, started, None, error, correlation_id)
            if isinstance(error, httpx.HTTPError | TimeoutError):
                raise ExecutionFailed(
                    f"{operation.describe()} failed: {type(error).__name__}"
                ) from error
            raise
        await self._finish(operation, execution_id, started, result, None, correlation_id)
        return result

    async def _send(
        self,
        operation: Operation,
        grant: Authorization,
        payload: HttpInput,
        timeout: float,
        correlation_id: UUID | None,
    ) -> HttpResult:
        policy = self._policy
        method = payload.method
        url = payload.url
        headers = dict(payload.headers)
        body = payload.body
        redirects: list[str] = []
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
            ) as client,
        ):
            while True:
                problem = _check_url(url, policy)
                if problem is not None:
                    raise await self._permissions.reject(
                        operation,
                        BlockReason.POLICY_VIOLATION,
                        f"redirect refused: {problem}" if redirects else problem,
                        decision_id=grant.decision_id,
                        correlation_id=correlation_id,
                    )
                request = client.build_request(method, url, headers=headers, content=body)
                response = await client.send(request, stream=True)
                try:
                    if response.is_redirect and method in _REDIRECTABLE_METHODS:
                        if len(redirects) >= policy.max_redirects:
                            raise await self._permissions.reject(
                                operation,
                                BlockReason.POLICY_VIOLATION,
                                "too many redirects",
                                decision_id=grant.decision_id,
                                correlation_id=correlation_id,
                            )
                        next_url = str(response.url.join(response.headers["location"]))
                        if url_host(next_url) != url_host(url):
                            headers = {
                                name: value
                                for name, value in headers.items()
                                if name.lower() not in _CROSS_HOST_DROPPED_HEADERS
                            }
                        redirects.append(next_url)
                        url = next_url
                        continue
                    content, truncated = await _read_capped(response, policy.max_response_bytes)
                    encoding = response.charset_encoding or "utf-8"
                    return HttpResult(
                        status_code=response.status_code,
                        url=str(response.url),
                        headers=dict(response.headers),
                        body=UntrustedContent(
                            source=str(response.url),
                            content=content.decode(encoding, errors="replace"),
                        ),
                        truncated=truncated,
                        redirects=tuple(redirects),
                    )
                finally:
                    await response.aclose()

    async def _finish(
        self,
        operation: Operation,
        execution_id: UUID,
        started: float,
        result: HttpResult | None,
        error: BaseException | None,
        correlation_id: UUID | None,
    ) -> None:
        details: dict[str, Any] = {}
        if result is not None:
            details = {
                "status_code": result.status_code,
                "bytes": len(result.body.content),
                "truncated": result.truncated,
                "redirects": len(result.redirects),
            }
        if error is not None:
            details["error"] = type(error).__name__
        await self._record(
            operation,
            "execution.finished",
            {
                "execution_id": execution_id,
                "success": result is not None and result.status_code < 400,
                "duration_seconds": max(time.monotonic() - started, 0.0),
                "details": details,
            },
            execution_id,
            correlation_id,
        )

    async def _record(
        self,
        operation: Operation,
        event_type: str,
        payload: dict[str, Any],
        execution_id: UUID,
        correlation_id: UUID | None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            stage_event(
                session,
                self._recorder,
                event_type,
                source=SOURCE,
                actor=operation.requested_by.value,
                aggregate_type="execution_run",
                aggregate_id=execution_id,
                payload=payload,
                objective_id=operation.objective_id,
                task_id=operation.task_id,
                correlation_id=correlation_id,
            )


async def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        room = cap - len(buffer)
        if len(chunk) > room:
            buffer.extend(chunk[:room])
            return bytes(buffer), True
        buffer.extend(chunk)
    return bytes(buffer), False
