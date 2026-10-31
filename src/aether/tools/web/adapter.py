"""SafeWeb: read-only web research tools for SCOUT and ROGUE.

Every network fetch, including robots.txt checks, crawl pages and follow-up API calls, is its
own ``web_fetch`` ``Operation`` authorized by the ``PermissionService`` and sent through the
pinned SafeHttp exchange (allowlist re-checked per redirect hop, DNS pinned, private and
reserved addresses refused). Responses come back as ``UntrustedContent``: page text is data to
cite, never instructions, and nothing here can turn it into an operation.
"""

import asyncio
import hashlib
import json
import re
import socket
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote, urlencode, urljoin, urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import EventRecorder
from aether.security.errors import BlockReason, OperationBlocked
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import host_matches, url_host
from aether.security.untrusted import UntrustedContent
from aether.tools.adapters._base import ExecutionFailed, admit
from aether.tools.adapters.http import Resolver, SafeHttp
from aether.tools.registry import OperationSpec
from aether.tools.web.cache import WebCache
from aether.tools.web.extract import ExtractedPage, Link, extract_html, normalize_url
from aether.tools.web.politeness import GitHubRateLimit, HostRateLimiter
from aether.tools.web.robots import (
    MAX_ROBOTS_BYTES,
    RobotsCache,
    RobotsRules,
    disallow_all,
    rules_for_response,
)
from aether.tools.web.settings import WebSettings
from aether.tools.web.sources import (
    DocHit,
    parse_objects_inv,
    parse_searchindex,
    render_doc_hits,
    render_github_repo,
    render_github_search,
    render_pypi,
    search_inventory,
    search_searchindex,
    summarize_github_release,
    summarize_github_repo,
    summarize_github_search,
    summarize_pypi,
)

SOURCE = "aether.tools.web"
GITHUB_API = "https://api.github.com"
PYPI_API = "https://pypi.org/pypi"
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_JSON_TYPES = frozenset({"application/json"})
_PYPI_NAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_PYPI_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}$")
_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class UnacceptableContent(Exception):
    """The response's media type is not one this tool may read."""


class RateLimited(ExecutionFailed):
    """A remote API's published rate limit is exhausted; the call was not sent."""


# -- tool inputs --------------------------------------------------------------------------------
class _WebInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_name: ClassVar[str]


def _absolute_http_url(value: str) -> str:
    url = normalize_url(value)
    if url is None or url != value.strip():
        raise ValueError("url must be an absolute http(s) URL without credentials or fragment")
    return url


class _PageInput(_WebInput):
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _absolute_http_url(value)


class WebFetchInput(_PageInput):
    tool_name: ClassVar[str] = "web.fetch"


class WebExtractInput(_PageInput):
    tool_name: ClassVar[str] = "web.extract"
    max_chars: int | None = Field(default=None, ge=256, le=1_000_000)


class WebLinksInput(_PageInput):
    """Links from ``url``; with ``depth`` > 0 a bounded same-host breadth-first crawl."""

    tool_name: ClassVar[str] = "web.links"
    depth: int = Field(default=0, ge=0, le=3)
    max_pages: int = Field(default=5, ge=1, le=20)


class PypiPackageInput(_WebInput):
    tool_name: ClassVar[str] = "pypi.package"
    name: str = Field(min_length=1, max_length=100, pattern=_PYPI_NAME.pattern)
    version: str | None = Field(default=None, max_length=64, pattern=_PYPI_VERSION.pattern)


class GithubRepoInput(_WebInput):
    tool_name: ClassVar[str] = "github.repo"
    owner: str = Field(min_length=1, max_length=39, pattern=_GITHUB_OWNER.pattern)
    repo: str = Field(min_length=1, max_length=100, pattern=_GITHUB_REPO.pattern)
    include_release: bool = True

    @field_validator("repo")
    @classmethod
    def _no_dot_segments(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("repo must be a repository name")
        return value


class GithubSearchInput(_WebInput):
    tool_name: ClassVar[str] = "github.search"
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=5, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _single_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must not be blank")
        return value


class DocsSearchInput(_WebInput):
    """Search a Sphinx documentation site through its ``objects.inv`` or ``searchindex.js``."""

    tool_name: ClassVar[str] = "docs.search"
    base_url: str = Field(min_length=1, max_length=1024)
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=50)

    @field_validator("base_url")
    @classmethod
    def _directory_url(cls, value: str) -> str:
        url = _absolute_http_url(value)
        parts = urlsplit(url)
        if parts.query:
            raise ValueError("base_url must not have a query string")
        return url if url.endswith("/") else url + "/"

    @field_validator("query")
    @classmethod
    def _single_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("query must not be blank")
        return value


WEB_INPUTS: dict[str, type[_WebInput]] = {
    model.tool_name: model
    for model in (
        WebFetchInput,
        WebExtractInput,
        WebLinksInput,
        PypiPackageInput,
        GithubRepoInput,
        GithubSearchInput,
        DocsSearchInput,
    )
}


# -- results ------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PageRecord:
    url: str
    status_code: int
    title: str
    sha256: str
    depth: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class WebResult:
    """Outcome of one web tool call. ``body`` is the citable untrusted evidence text.

    ``raw_sha256`` hashes the exact bytes received for the primary URL; ``body.content_hash``
    hashes the rendered evidence text. Every string field is untrusted remote data.
    """

    tool: str
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: UntrustedContent
    truncated: bool
    raw_sha256: str
    from_cache: bool
    title: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    links: tuple[Link, ...] = ()
    pages: tuple[PageRecord, ...] = ()
    hits: tuple[DocHit, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def suspicious_patterns(self) -> tuple[str, ...]:
        return self.body.suspicious_patterns()


@dataclass(frozen=True, slots=True)
class _Fetched:
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    sha256: str
    truncated: bool
    from_cache: bool

    @property
    def media_type(self) -> str:
        return self.content_type.split(";", 1)[0].strip().lower()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def text(self) -> str:
        charset = "utf-8"
        for parameter in self.content_type.split(";")[1:]:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "charset" and value.strip():
                charset = value.strip().strip("\"'")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8", errors="strict"))


@dataclass(slots=True)
class _Call:
    tool: str
    operation: Operation
    grant: Authorization
    correlation_id: UUID | None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


# -- adapter ------------------------------------------------------------------------------------
class SafeWeb(SafeHttp):
    kinds = frozenset({OperationKind.WEB_FETCH})
    source = SOURCE

    def __init__(
        self,
        permissions: PermissionService,
        sessions: async_sessionmaker[AsyncSession],
        recorder: EventRecorder,
        settings: WebSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = socket.getaddrinfo,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        super().__init__(
            permissions, sessions, recorder, transport=transport, resolver=resolver, clock=clock
        )
        self.settings = settings or WebSettings()
        self.cache = WebCache(
            permissions.policy.data_dir / "web_cache",
            self.settings.cache_ttl_seconds,
            clock=wall_clock,
        )
        self.robots = RobotsCache(self.settings.robots_ttl_seconds, clock=monotonic)
        self.limiter = HostRateLimiter(
            self.settings.per_host_interval_seconds, clock=monotonic, sleep=sleep
        )
        self.github_limits = GitHubRateLimit(clock=wall_clock)
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._page_types = frozenset(self.settings.allowed_content_types)

    # -- operation description ---------------------------------------------------------------
    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec:
        if kind is not OperationKind.WEB_FETCH or not isinstance(payload, _WebInput):
            raise TypeError("SafeWeb expects a web research tool input")
        target = self._primary_url(payload)
        host = url_host(target)
        if host is None:
            raise ValueError("url must be an absolute http(s) URL")
        if isinstance(payload, DocsSearchInput) and not self._is_docs_host(host):
            raise ValueError(f"host {host!r} is not a configured documentation host")
        return OperationSpec(
            target=target,
            arguments={"tool": payload.tool_name, **payload.model_dump(mode="json")},
            hosts=(host,),
        )

    def _primary_url(self, payload: _WebInput) -> str:
        if isinstance(payload, WebFetchInput | WebExtractInput | WebLinksInput):
            return payload.url
        if isinstance(payload, PypiPackageInput):
            name = quote(payload.name, safe="")
            if payload.version:
                return f"{PYPI_API}/{name}/{quote(payload.version, safe='')}/json"
            return f"{PYPI_API}/{name}/json"
        if isinstance(payload, GithubRepoInput):
            return f"{GITHUB_API}/repos/{payload.owner}/{payload.repo}"
        if isinstance(payload, GithubSearchInput):
            query = urlencode({"q": payload.query, "per_page": payload.limit})
            return f"{GITHUB_API}/search/repositories?{query}"
        if isinstance(payload, DocsSearchInput):
            return urljoin(payload.base_url, "objects.inv")
        raise TypeError(f"unsupported web tool input {type(payload).__name__}")

    def _is_docs_host(self, host: str) -> bool:
        return host_matches(host, frozenset(self.settings.docs_hosts))

    # -- execution ---------------------------------------------------------------------------
    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> WebResult:
        operation, grant = await admit(
            self._permissions, self.kinds, operation, authorization, correlation_id=correlation_id
        )
        arguments = dict(operation.arguments)
        tool = arguments.pop("tool", None)
        try:
            model = WEB_INPUTS[str(tool)]
            payload = model.model_validate(arguments)
            spec = self.describe(operation.kind, payload)
        except (KeyError, ValidationError, ValueError, TypeError) as error:
            raise await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                f"malformed web tool arguments: {type(error).__name__}",
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            ) from error
        if spec.target != operation.target or spec.arguments != operation.arguments:
            raise await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                "web tool request does not match the authorized operation",
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            )
        call = _Call(payload.tool_name, operation, grant, correlation_id)
        if isinstance(payload, WebFetchInput):
            return await self._web_fetch(call, payload)
        if isinstance(payload, WebExtractInput):
            return await self._web_extract(call, payload)
        if isinstance(payload, WebLinksInput):
            return await self._web_links(call, payload)
        if isinstance(payload, PypiPackageInput):
            return await self._pypi_package(call)
        if isinstance(payload, GithubRepoInput):
            return await self._github_repo(call, payload)
        if isinstance(payload, GithubSearchInput):
            return await self._github_search(call, payload)
        assert isinstance(payload, DocsSearchInput)
        return await self._docs_search(call, payload)

    # -- tools -------------------------------------------------------------------------------
    async def _web_fetch(self, call: _Call, payload: WebFetchInput) -> WebResult:
        fetched = await self._fetch(
            call, payload.url, purpose="page", accept=self._page_types, robots=True, primary=True
        )
        return self._result(call, fetched, fetched.text(), truncated=fetched.truncated)

    async def _web_extract(self, call: _Call, payload: WebExtractInput) -> WebResult:
        fetched = await self._fetch(
            call, payload.url, purpose="page", accept=self._page_types, robots=True, primary=True
        )
        limit = min(payload.max_chars or self.settings.max_extract_chars, 1_000_000)
        if fetched.media_type in _HTML_TYPES:
            page = self._extract(fetched, limit)
            return self._result(
                call,
                fetched,
                page.render(),
                truncated=fetched.truncated or page.truncated,
                title=page.title,
                facts={"metadata": dict(page.metadata), "headings": list(page.headings)},
                links=page.links,
            )
        text = fetched.text()
        truncated = fetched.truncated or len(text) > limit
        return self._result(call, fetched, text[:limit], truncated=truncated)

    async def _web_links(self, call: _Call, payload: WebLinksInput) -> WebResult:
        depth = min(payload.depth, self.settings.max_crawl_depth)
        max_pages = min(payload.max_pages, self.settings.max_crawl_pages)
        start_host = url_host(payload.url)
        first = await self._fetch(
            call, payload.url, purpose="page", accept=self._page_types, robots=True, primary=True
        )
        links: dict[str, Link] = {}
        pages: list[PageRecord] = []
        notes: list[str] = []
        queue: list[tuple[str, int]] = []
        seen = {payload.url}

        def absorb(fetched: _Fetched, level: int) -> None:
            if fetched.media_type not in _HTML_TYPES or not fetched.ok:
                pages.append(
                    PageRecord(fetched.url, fetched.status_code, "", fetched.sha256, level)
                )
                return
            page = self._extract(fetched, self.settings.max_extract_chars)
            pages.append(
                PageRecord(fetched.url, fetched.status_code, page.title, fetched.sha256, level)
            )
            for link in page.links:
                if len(links) < self.settings.max_links:
                    links.setdefault(link.url, link)
                if (
                    level < depth
                    and not page.nofollow
                    and link.url not in seen
                    and url_host(link.url) == start_host
                ):
                    seen.add(link.url)
                    queue.append((link.url, level + 1))

        absorb(first, 0)
        while queue and len(pages) < max_pages:
            url, level = queue.pop(0)
            try:
                fetched = await self._fetch(
                    call, url, purpose="page", accept=self._page_types, robots=True
                )
            except OperationBlocked as error:
                pages.append(PageRecord(url, 0, "", "", level, note=f"blocked: {error.detail}"))
                continue
            except ExecutionFailed as error:
                pages.append(PageRecord(url, 0, "", "", level, note=f"failed: {error}"))
                continue
            absorb(fetched, level)
        if queue:
            notes.append(f"crawl stopped at {max_pages} pages; {len(queue)} links not visited")
        rendered = [f"Links found from {payload.url} ({len(pages)} pages, {len(links)} links)"]
        rendered.extend(f"- {link.url} {link.text}".rstrip() for link in links.values())
        return self._result(
            call,
            first,
            "\n".join(rendered),
            truncated=first.truncated or len(links) >= self.settings.max_links,
            links=tuple(links.values()),
            pages=tuple(pages),
            notes=tuple(notes),
        )

    async def _pypi_package(self, call: _Call) -> WebResult:
        fetched = await self._fetch(call, call.operation.target, purpose="api", accept=_JSON_TYPES)
        if not fetched.ok:
            text = f"PyPI returned HTTP {fetched.status_code} for {call.operation.target}"
            return self._result(call, fetched, text, truncated=fetched.truncated)
        facts = summarize_pypi(self._json(fetched))
        return self._result(
            call, fetched, render_pypi(facts), truncated=False, title=facts["name"], facts=facts
        )

    async def _github_repo(self, call: _Call, payload: GithubRepoInput) -> WebResult:
        fetched = await self._fetch(
            call, call.operation.target, purpose="api", accept=_JSON_TYPES, headers=_GITHUB_HEADERS
        )
        if not fetched.ok:
            text = f"GitHub returned HTTP {fetched.status_code} for {call.operation.target}"
            return self._result(call, fetched, text, truncated=fetched.truncated)
        facts = summarize_github_repo(self._json(fetched))
        release = None
        notes: list[str] = []
        if payload.include_release:
            try:
                latest = await self._fetch(
                    call,
                    f"{call.operation.target}/releases/latest",
                    purpose="release",
                    accept=_JSON_TYPES,
                    headers=_GITHUB_HEADERS,
                )
                if latest.ok:
                    release = summarize_github_release(self._json(latest))
            except (OperationBlocked, ExecutionFailed) as error:
                notes.append(f"latest release unavailable: {type(error).__name__}")
        if release is not None:
            facts["latest_release"] = release
        return self._result(
            call,
            fetched,
            render_github_repo(facts, release),
            truncated=False,
            title=facts["full_name"],
            facts=facts,
            notes=tuple(notes),
        )

    async def _github_search(self, call: _Call, payload: GithubSearchInput) -> WebResult:
        fetched = await self._fetch(
            call, call.operation.target, purpose="api", accept=_JSON_TYPES, headers=_GITHUB_HEADERS
        )
        if not fetched.ok:
            text = f"GitHub search returned HTTP {fetched.status_code}"
            return self._result(call, fetched, text, truncated=fetched.truncated)
        limit = min(payload.limit, self.settings.max_search_results)
        hits = summarize_github_search(self._json(fetched), limit)
        return self._result(
            call,
            fetched,
            render_github_search(payload.query, hits),
            truncated=False,
            facts={"query": payload.query, "repositories": hits},
        )

    async def _docs_search(self, call: _Call, payload: DocsSearchInput) -> WebResult:
        limit = min(payload.limit, self.settings.max_search_results)
        notes: list[str] = []
        inventory = await self._fetch(
            call, call.operation.target, purpose="inventory", accept=None, robots=True, primary=True
        )
        if inventory.ok:
            try:
                entries = parse_objects_inv(inventory.body, payload.base_url)
                hits = search_inventory(entries, payload.query, limit)
                text = render_doc_hits(payload.base_url, payload.query, hits, "objects.inv")
                return self._result(
                    call, inventory, text, truncated=inventory.truncated, hits=tuple(hits)
                )
            except (ValueError, zlib.error) as error:
                notes.append(f"objects.inv unusable: {type(error).__name__}")
        else:
            notes.append(f"objects.inv returned HTTP {inventory.status_code}")
        index_url = urljoin(payload.base_url, "searchindex.js")
        try:
            fetched = await self._fetch(
                call, index_url, purpose="searchindex", accept=None, robots=True
            )
        except (OperationBlocked, ExecutionFailed) as error:
            notes.append(f"searchindex.js unavailable: {type(error).__name__}")
            text = render_doc_hits(payload.base_url, payload.query, [], "none")
            return self._result(call, inventory, text, truncated=False, notes=tuple(notes))
        hits: list[DocHit] = []
        if fetched.ok and not fetched.truncated:
            try:
                index = parse_searchindex(fetched.text())
                hits = search_searchindex(index, payload.base_url, payload.query, limit)
            except ValueError as error:
                notes.append(f"searchindex.js unusable: {type(error).__name__}")
        elif fetched.truncated:
            notes.append("searchindex.js exceeds the page size limit")
        else:
            notes.append(f"searchindex.js returned HTTP {fetched.status_code}")
        text = render_doc_hits(payload.base_url, payload.query, hits, "searchindex.js")
        return self._result(
            call, fetched, text, truncated=False, hits=tuple(hits), notes=tuple(notes)
        )

    # -- helpers -----------------------------------------------------------------------------
    def _extract(self, fetched: _Fetched, limit: int) -> ExtractedPage:
        return extract_html(
            fetched.text(), fetched.final_url, max_chars=limit, max_links=self.settings.max_links
        )

    @staticmethod
    def _json(fetched: _Fetched) -> Any:
        try:
            return fetched.json()
        except ValueError as error:
            raise ExecutionFailed(f"{fetched.url} did not return valid JSON") from error

    def _result(
        self,
        call: _Call,
        fetched: _Fetched,
        text: str,
        *,
        truncated: bool,
        title: str = "",
        facts: dict[str, Any] | None = None,
        links: tuple[Link, ...] = (),
        pages: tuple[PageRecord, ...] = (),
        hits: tuple[DocHit, ...] = (),
        notes: tuple[str, ...] = (),
    ) -> WebResult:
        return WebResult(
            tool=call.tool,
            url=fetched.url,
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            content_type=fetched.media_type,
            body=UntrustedContent(source=fetched.final_url[:2048], content=text),
            truncated=truncated,
            raw_sha256=fetched.sha256,
            from_cache=fetched.from_cache,
            title=title,
            facts=facts or {},
            links=links,
            pages=pages,
            hits=hits,
            notes=notes,
        )

    async def _authorize_follow_up(
        self, call: _Call, url: str, purpose: str
    ) -> tuple[Operation, Authorization]:
        host = url_host(url)
        follow_up = Operation(
            kind=OperationKind.WEB_FETCH,
            target=url,
            arguments={"tool": call.tool, "purpose": purpose, "url": url},
            hosts=(host,) if host else (),
            requested_by=call.operation.requested_by,
            task_id=call.operation.task_id,
            objective_id=call.operation.objective_id,
            declared_requirement_id=call.operation.declared_requirement_id,
        )
        grant = await self._permissions.authorize(follow_up, correlation_id=call.correlation_id)
        return await admit(
            self._permissions,
            self.kinds,
            follow_up,
            grant,
            correlation_id=call.correlation_id,
        )

    async def _robots_rules(self, call: _Call, url: str) -> RobotsRules:
        origin = _origin(url)
        rules = self.robots.get(origin)
        if rules is not None:
            return rules
        try:
            fetched = await self._fetch(
                call,
                f"{origin}/robots.txt",
                purpose="robots",
                accept=None,
                max_bytes=MAX_ROBOTS_BYTES,
            )
            rules = rules_for_response(fetched.status_code, fetched.media_type, fetched.text())
        except OperationBlocked as error:
            rules = disallow_all(f"robots.txt fetch blocked: {error.detail}")
        except ExecutionFailed as error:
            rules = disallow_all(f"robots.txt unreachable: {error}")
        delay = rules.crawl_delay(self.settings.user_agent)
        host = url_host(url)
        if delay and host:
            self.limiter.set_crawl_delay(host, delay)
        self.robots.put(origin, rules)
        return rules

    async def _fetch(
        self,
        call: _Call,
        url: str,
        *,
        purpose: str,
        accept: frozenset[str] | None,
        robots: bool = False,
        primary: bool = False,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
    ) -> _Fetched:
        """One audited GET. ``primary`` uses the call's own grant; anything else is authorized
        as a separate follow-up operation first."""
        if primary:
            operation, grant = call.operation, call.grant
        else:
            operation, grant = await self._authorize_follow_up(call, url, purpose)
        if robots:
            rules = await self._robots_rules(call, url)
            if not rules.allows(url, self.settings.user_agent):
                raise await self._permissions.reject(
                    operation,
                    BlockReason.POLICY_VIOLATION,
                    f"robots.txt disallows this URL ({rules.reason})",
                    decision_id=grant.decision_id,
                    correlation_id=call.correlation_id,
                )
        execution_id = uuid4()
        await self._record(
            operation,
            "execution.started",
            {"execution_id": execution_id, "operation": operation.describe()},
            execution_id,
            call.correlation_id,
            grant=grant,
        )
        started = time.monotonic()
        details: dict[str, Any] = {"tool": call.tool, "purpose": purpose}
        cached = self.cache.get(purpose, url)
        if cached is not None:
            details.update(cache="hit", bytes=len(cached.body), sha256=cached.sha256)
            await self._finish_run(
                operation,
                execution_id,
                started,
                cached.status_code,
                details,
                None,
                call.correlation_id,
            )
            return _Fetched(
                url,
                cached.final_url,
                cached.status_code,
                cached.content_type,
                cached.body,
                cached.sha256,
                cached.truncated,
                True,
            )
        details["cache"] = "miss"
        try:
            fetched = await self._send_get(operation, grant, call, url, accept, headers, max_bytes)
        except (asyncio.CancelledError, Exception) as error:
            await asyncio.shield(
                self._finish_run(
                    operation, execution_id, started, None, details, error, call.correlation_id
                )
            )
            if isinstance(error, UnacceptableContent):
                raise await self._permissions.reject(
                    operation,
                    BlockReason.POLICY_VIOLATION,
                    str(error),
                    decision_id=grant.decision_id,
                    correlation_id=call.correlation_id,
                ) from error
            if not isinstance(error, OperationBlocked) and isinstance(
                error, httpx.HTTPError | TimeoutError | OSError
            ):
                raise ExecutionFailed(
                    f"{operation.describe()} failed: {type(error).__name__}"
                ) from error
            raise
        details.update(
            bytes=len(fetched.body),
            sha256=fetched.sha256,
            truncated=fetched.truncated,
            content_type=fetched.media_type,
        )
        await self._finish_run(
            operation,
            execution_id,
            started,
            fetched.status_code,
            details,
            None,
            call.correlation_id,
        )
        if fetched.status_code == 200:
            self.cache.put(
                purpose,
                url,
                final_url=fetched.final_url,
                status_code=fetched.status_code,
                content_type=fetched.content_type,
                body=fetched.body,
                truncated=fetched.truncated,
            )
        return fetched

    async def _send_get(
        self,
        operation: Operation,
        grant: Authorization,
        call: _Call,
        url: str,
        accept: frozenset[str] | None,
        headers: dict[str, str] | None,
        max_bytes: int | None,
    ) -> _Fetched:
        host = url_host(url) or ""
        bucket = _github_bucket(url)
        if bucket is not None and (wait := self.github_limits.blocked_for(bucket)) > 0:
            raise RateLimited(f"GitHub {bucket} API rate limit exhausted; retry in {wait:.0f}s")
        policy = self._policy
        timeout = min(self.settings.timeout_seconds, policy.http_timeout_seconds)
        timeout = min(timeout, grant.limits.max_runtime_seconds or timeout)

        def inspect(response: httpx.Response) -> None:
            if accept is None or not 200 <= response.status_code < 300:
                return
            media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media not in accept:
                raise UnacceptableContent(f"content type {media or 'unknown'!r} is not allowed")

        request_headers = {"User-Agent": self.settings.user_agent, **(headers or {})}
        async with self._semaphore:
            await self.limiter.wait(host)
            raw = await self._exchange(
                operation,
                grant,
                method="GET",
                url=url,
                headers=request_headers,
                body=None,
                timeout=timeout,
                correlation_id=call.correlation_id,
                max_bytes=min(max_bytes or self.settings.max_page_bytes, policy.max_response_bytes),
                inspect=inspect,
            )
        if bucket is not None:
            self.github_limits.observe(bucket, raw.status_code, raw.headers)
        content_type = next(
            (value for name, value in raw.headers.items() if name.lower() == "content-type"), ""
        )
        return _Fetched(
            url=url,
            final_url=raw.url,
            status_code=raw.status_code,
            content_type=content_type,
            body=raw.content,
            sha256=hashlib.sha256(raw.content).hexdigest(),
            truncated=raw.truncated,
            from_cache=False,
        )


_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def _github_bucket(url: str) -> str | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "api.github.com":
        return None
    return "search" if parts.path.startswith("/search/") else "core"
