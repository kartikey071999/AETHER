"""SafeHttp: allowlisted HTTP(S) requests whose responses are returned as untrusted content."""

import asyncio
import ipaddress
import socket
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
from aether.security.errors import BlockReason, OperationBlocked
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import SecurityPolicy, url_host
from aether.security.untrusted import UntrustedContent
from aether.storage.models import RunStatus
from aether.storage.repositories.execution import ExecutionRunRepository
from aether.tools.adapters._base import ExecutionFailed, admit
from aether.tools.registry import OperationSpec

SOURCE = "aether.tools.http"
_SCHEMES = frozenset({"http", "https"})
_CROSS_HOST_DROPPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_REDIRECTABLE_METHODS = frozenset({"GET", "HEAD"})
AddrInfo = tuple[int, int, int, str, tuple[str, int] | tuple[str, int, int, int]]
Resolver = Callable[[str, int], list[AddrInfo]]


class _PinnedTransport(httpx.AsyncBaseTransport):
    """Connect to the validated address while preserving the logical Host and TLS SNI."""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        address = request.extensions["aether.resolved_ip"]
        headers = request.headers.copy()
        headers["host"] = request.url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions.pop("aether.resolved_ip")
        extensions["sni_hostname"] = request.url.raw_host.decode("ascii")
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await self._transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._transport.aclose()


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


@dataclass(frozen=True, slots=True)
class RawResponse:
    """Undecoded, size-capped response of one pinned exchange; the bytes are untrusted."""

    status_code: int
    url: str
    headers: dict[str, str]
    content: bytes
    truncated: bool
    redirects: tuple[str, ...]
    encoding: str | None


ResponseInspector = Callable[[httpx.Response], None]


def _check_url(
    url: str, policy: SecurityPolicy, kind: OperationKind = OperationKind.HTTP_REQUEST
) -> str | None:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return "URL is invalid"
    if parsed.scheme not in _SCHEMES:
        return f"scheme {parsed.scheme!r} is not allowed"
    if parsed.username or parsed.password:
        return "URL credentials are not allowed"
    host = url_host(url)
    if host is None or not policy.host_allowed_for(kind, host):
        return f"host {host!r} is not allowlisted"
    return None


def _explicitly_local(
    host: str, policy: SecurityPolicy, kind: OperationKind = OperationKind.HTTP_REQUEST
) -> bool:
    """Only exact localhost or literal local-IP entries express intent to contact local services.

    A wildcard or a public DNS name in allowed_hosts never authorizes private resolution, and
    web research (``web_fetch``) never contacts local services at all.
    """
    if kind is not OperationKind.HTTP_REQUEST or host not in policy.allowed_hosts:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


class SafeHttp:
    kinds = frozenset({OperationKind.HTTP_REQUEST})
    source = SOURCE

    def __init__(
        self,
        permissions: PermissionService,
        sessions: async_sessionmaker[AsyncSession],
        recorder: EventRecorder,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._permissions = permissions
        self._sessions = sessions
        self._recorder = recorder
        self._transport = transport
        self._resolver = resolver
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
        timeout = min(timeout, grant.limits.max_runtime_seconds or timeout)
        execution_id = uuid4()
        await self._record(
            operation,
            "execution.started",
            {"execution_id": execution_id, "operation": operation.describe()},
            execution_id,
            correlation_id,
            grant=grant,
        )
        started = time.monotonic()
        try:
            result = await self._send(operation, grant, payload, timeout, correlation_id)
        except (asyncio.CancelledError, Exception) as error:
            await asyncio.shield(
                self._finish(operation, execution_id, started, None, error, correlation_id)
            )
            if not isinstance(error, OperationBlocked) and isinstance(
                error, httpx.HTTPError | TimeoutError | OSError
            ):
                raise ExecutionFailed(
                    f"{operation.describe()} failed: {type(error).__name__}"
                ) from error
            raise
        await self._finish(operation, execution_id, started, result, None, correlation_id)
        return result

    async def _resolve(
        self,
        operation: Operation,
        grant: Authorization,
        url: str,
        correlation_id: UUID | None,
    ) -> str:
        parsed = httpx.URL(url)
        host = parsed.host
        entries = await asyncio.to_thread(
            self._resolver, host, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
        problem = "DNS resolution returned no addresses"
        addresses: list[str] = []
        for family, _, _, _, sockaddr in entries:
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                problem = "DNS resolution returned an invalid address"
                break
            checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
            checked = checked or address
            if (
                family not in {socket.AF_INET, socket.AF_INET6}
                or (family == socket.AF_INET) != (address.version == 4)
                or "%" in sockaddr[0]
                or checked.is_multicast
                or checked.is_reserved
                or checked.is_unspecified
                or (
                    not checked.is_global
                    and not _explicitly_local(host, self._policy, operation.kind)
                )
            ):
                problem = "DNS resolution returned a non-public or forbidden address"
                break
            addresses.append(str(address))
        else:
            if addresses:
                return addresses[0]
        raise await self._permissions.reject(
            operation,
            BlockReason.POLICY_VIOLATION,
            problem,
            decision_id=grant.decision_id,
            correlation_id=correlation_id,
        )

    async def _send(
        self,
        operation: Operation,
        grant: Authorization,
        payload: HttpInput,
        timeout: float,
        correlation_id: UUID | None,
    ) -> HttpResult:
        raw = await self._exchange(
            operation,
            grant,
            method=payload.method,
            url=payload.url,
            headers=dict(payload.headers),
            body=payload.body,
            timeout=timeout,
            correlation_id=correlation_id,
        )
        return HttpResult(
            status_code=raw.status_code,
            url=raw.url,
            headers=raw.headers,
            body=UntrustedContent(
                source=raw.url,
                content=raw.content.decode(raw.encoding or "utf-8", errors="replace"),
            ),
            truncated=raw.truncated,
            redirects=raw.redirects,
        )

    async def _exchange(
        self,
        operation: Operation,
        grant: Authorization,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
        timeout: float,
        correlation_id: UUID | None,
        max_bytes: int | None = None,
        inspect: ResponseInspector | None = None,
    ) -> RawResponse:
        """One pinned request chain: every hop re-checks the allowlist and resolved address.

        ``inspect`` sees the final response's status and headers before the body is read and
        may raise to refuse it (e.g. an unacceptable content type).
        """
        policy = self._policy
        redirects: list[str] = []
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(
                transport=_PinnedTransport(
                    self._transport
                    or httpx.AsyncHTTPTransport(
                        trust_env=False,
                        # Pools key by pinned IP, not logical host. Re-verify TLS on every hop.
                        limits=httpx.Limits(max_keepalive_connections=0),
                    )
                ),
                follow_redirects=False,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
            ) as client,
        ):
            while True:
                problem = _check_url(url, policy, operation.kind)
                if (
                    grant.limits.allowed_hosts is not None
                    and url_host(url) not in grant.limits.allowed_hosts
                ):
                    problem = "host is outside the allowed_hosts restriction"
                if problem is not None:
                    raise await self._permissions.reject(
                        operation,
                        BlockReason.POLICY_VIOLATION,
                        f"redirect refused: {problem}" if redirects else problem,
                        decision_id=grant.decision_id,
                        correlation_id=correlation_id,
                    )
                request = client.build_request(method, url, headers=headers, content=body)
                request.extensions["aether.resolved_ip"] = await self._resolve(
                    operation, grant, url, correlation_id
                )
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
                        current, next_target = httpx.URL(url), httpx.URL(next_url)
                        if (current.scheme, current.host, current.port) != (
                            next_target.scheme,
                            next_target.host,
                            next_target.port,
                        ):
                            headers = {
                                name: value
                                for name, value in headers.items()
                                if name.lower() not in _CROSS_HOST_DROPPED_HEADERS
                            }
                        redirects.append(next_url)
                        url = next_url
                        continue
                    cap = min(
                        policy.max_response_bytes,
                        grant.limits.max_output_bytes or policy.max_response_bytes,
                        max_bytes or policy.max_response_bytes,
                    )
                    if inspect is not None:
                        inspect(response)
                    content, truncated = await _read_capped(response, cap)
                    return RawResponse(
                        status_code=response.status_code,
                        url=str(response.url),
                        headers=dict(response.headers),
                        content=content,
                        truncated=truncated,
                        redirects=tuple(redirects),
                        encoding=response.charset_encoding,
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
        await self._finish_run(
            operation,
            execution_id,
            started,
            result.status_code if result is not None else None,
            details,
            error,
            correlation_id,
        )

    async def _finish_run(
        self,
        operation: Operation,
        execution_id: UUID,
        started: float,
        status_code: int | None,
        details: dict[str, Any],
        error: BaseException | None,
        correlation_id: UUID | None,
    ) -> None:
        details = dict(details)
        if error is not None:
            details["error"] = type(error).__name__
        succeeded = error is None and status_code is not None and status_code < 400
        status = (
            RunStatus.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            else RunStatus.SUCCEEDED
            if succeeded
            else RunStatus.FAILED
        )
        details["status"] = status.value
        await self._record(
            operation,
            "execution.finished",
            {
                "execution_id": execution_id,
                "success": succeeded,
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
        *,
        grant: Authorization | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            if event_type == "execution.started":
                await ExecutionRunRepository.stage_started(
                    session,
                    run_id=execution_id,
                    agent=operation.requested_by.value,
                    operation=operation.describe(),
                    started_at=self._clock(),
                    task_id=operation.task_id,
                    security_decision_id=grant.decision_id if grant else None,
                )
            else:
                await ExecutionRunRepository.stage_finished(
                    session,
                    execution_id,
                    status=RunStatus(payload["details"]["status"]),
                    finished_at=self._clock(),
                    exit_code=None,
                    resource_usage={
                        "duration_seconds": payload["duration_seconds"],
                        **payload["details"],
                    },
                )
            stage_event(
                session,
                self._recorder,
                event_type,
                source=self.source,
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
