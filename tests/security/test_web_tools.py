"""SafeWeb research tools: every fetch authorized, pinned, polite, cached and untrusted."""

import hashlib
import socket
from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy import select
from web_fixtures import (
    DOCS_HOST,
    WEB_HOSTS,
    MockWeb,
    fixture_bytes,
    logical_host,
    public_resolver,
    resolver_mapping,
)

from aether.communication.messages import AgentId
from aether.observability.recorder import EventRecorder
from aether.security import OperationBlocked, PermissionService, SecurityPolicy
from aether.security.errors import BlockReason
from aether.security.untrusted import UntrustedContent
from aether.storage.database import Database
from aether.storage.models import EventRecord, ExecutionRunRecord
from aether.tools import ToolInputError, ToolRegistry
from aether.tools.web import RateLimited, SafeWeb, WebResult, WebSettings, register_web_tools

ARTICLE = f"https://{DOCS_HOST}/guide/article.html"
EventsOf = Callable[[str], Awaitable[list[EventRecord]]]


@pytest.fixture
def policy(policy_factory: Callable[..., SecurityPolicy]) -> SecurityPolicy:
    return policy_factory(max_response_bytes=1024 * 1024, web_allowed_hosts=WEB_HOSTS)


@pytest.fixture
def web() -> MockWeb:
    return MockWeb()


@pytest.fixture
def sleeps() -> list[float]:
    return []


def build(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
    *,
    resolver=public_resolver,
    **settings: object,
) -> tuple[ToolRegistry, SafeWeb]:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    options: dict[str, object] = {
        "allowed_hosts": list(WEB_HOSTS),
        "docs_hosts": [DOCS_HOST, "*.readthedocs.io"],
    }
    options.update(settings)
    adapter = SafeWeb(
        permissions,
        database.sessions,
        recorder,
        WebSettings(_env_file=None, **options),
        transport=web.transport,
        resolver=resolver,
        monotonic=lambda: 0.0,
        sleep=fake_sleep,
    )
    registry = ToolRegistry(permissions)
    register_web_tools(registry, adapter)
    return registry, adapter


@pytest.fixture
def tools(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
) -> ToolRegistry:
    return build(permissions, database, recorder, web, sleeps)[0]


async def runs(database: Database) -> list[ExecutionRunRecord]:
    async with database.sessions() as session:
        return list(await session.scalars(select(ExecutionRunRecord)))


async def test_extract_returns_readable_untrusted_text_with_provenance(
    tools: ToolRegistry, web: MockWeb, events_of: EventsOf
) -> None:
    result = await tools.invoke(AgentId.SCOUT, "web.extract", {"url": ARTICLE})

    assert isinstance(result, WebResult) and isinstance(result.body, UntrustedContent)
    assert result.status_code == 200 and result.content_type == "text/html"
    assert result.body.source == ARTICLE
    assert result.raw_sha256 == hashlib.sha256(fixture_bytes("article.html")).hexdigest()
    text = result.body.content
    assert result.title == "Streaming Responses | Example HTTP Guide"
    assert "Description: How to stream large HTTP responses" in text
    assert "# Streaming responses" in text and "## Iterating bytes" in text
    assert "- Call iter_bytes() for raw chunks." in text
    assert "    for chunk in response.iter_bytes():\n        handle(chunk)" in text
    for noise in (
        "TRACKING SCRIPT",
        "STYLE TEXT",
        "NAVIGATION BOILERPLATE",
        "FOOTER BOILERPLATE",
        "HIDDEN DIV",
        "INVISIBLE PARAGRAPH",
        "ARIA HIDDEN",
        "NOSCRIPT TEXT",
        "IFRAME TEXT",
    ):
        assert noise not in text
    assert [link.url for link in result.links] == [
        f"https://{DOCS_HOST}/guide/index.html",
        "https://pypi.org/project/example-http/",
        f"https://{DOCS_HOST}/guide/timeouts.html",
        "https://github.com/example/http",
    ]
    assert result.facts["metadata"]["canonical"] == ARTICLE
    assert result.facts["metadata"]["generator"] == "Sphinx 7.2.6"
    assert web.paths(DOCS_HOST) == ["/robots.txt", "/guide/article.html"]
    request = web.requests[-1]
    assert request.method == "GET" and request.headers["user-agent"].startswith("AETHER-research/")
    # robots.txt and the page are two separately authorized and audited operations.
    authorized = await events_of("security.operation_authorized")
    assert [event.payload["operation"] for event in authorized] == [
        f"web_fetch:{ARTICLE}",
        f"web_fetch:https://{DOCS_HOST}/robots.txt",
    ]
    assert len(await events_of("execution.finished")) == 2


async def test_robots_disallow_is_honoured_and_recorded(
    tools: ToolRegistry, web: MockWeb, events_of: EventsOf
) -> None:
    with pytest.raises(OperationBlocked) as blocked:
        await tools.invoke(
            AgentId.SCOUT, "web.fetch", {"url": f"https://{DOCS_HOST}/private/internal.html"}
        )

    assert blocked.value.reason is BlockReason.POLICY_VIOLATION
    assert "robots.txt disallows" in blocked.value.detail
    assert web.paths() == ["/robots.txt"]
    events = await events_of("security.operation_blocked")
    assert any("robots.txt" in str(event.payload) for event in events)


async def test_robots_server_error_disallows_everything(tools: ToolRegistry, web: MockWeb) -> None:
    web.overrides[(DOCS_HOST, "/robots.txt")] = httpx.Response(503, text="down")

    with pytest.raises(OperationBlocked, match="server error"):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})
    assert web.paths() == ["/robots.txt"]


async def test_missing_robots_allows_crawling(tools: ToolRegistry, web: MockWeb) -> None:
    web.overrides[(DOCS_HOST, "/robots.txt")] = httpx.Response(404, text="none")

    result = await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})

    assert result.status_code == 200 and "Streaming responses" in result.body.content


async def test_per_host_rate_limit_and_crawl_delay_are_enforced(
    tools: ToolRegistry, web: MockWeb, sleeps: list[float]
) -> None:
    await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})
    await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": f"https://{DOCS_HOST}/guide/index.html"})
    await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})

    # The clock never advances: robots.txt goes first, the article waits the 1s default
    # interval, then the robots.txt Crawl-delay of 2s spaces later requests. pypi.org is a
    # different host and is not delayed.
    assert sleeps == [1.0, 3.0]
    assert web.paths() == [
        "/robots.txt",
        "/guide/article.html",
        "/guide/index.html",
        "/pypi/example-http/json",
    ]


async def test_disk_cache_hit_skips_the_network_but_is_still_audited(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
    policy: SecurityPolicy,
) -> None:
    first_tools, _ = build(permissions, database, recorder, web, sleeps)
    first = await first_tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})
    # A fresh adapter (e.g. after a restart) reads the same on-disk cache.
    second_tools, _ = build(permissions, database, recorder, web, sleeps)
    second = await second_tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})

    assert web.paths("pypi.org") == ["/pypi/example-http/json"]
    assert not first.from_cache and second.from_cache
    assert first.raw_sha256 == second.raw_sha256
    assert first.body.content_hash == second.body.content_hash
    assert any((policy.data_dir / "web_cache").rglob("*.body"))
    recorded = await runs(database)
    assert len(recorded) == 2
    assert sorted(run.resource_usage["cache"] for run in recorded) == ["hit", "miss"]


async def test_tampered_cache_entry_is_refetched(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
    policy: SecurityPolicy,
) -> None:
    tools, _ = build(permissions, database, recorder, web, sleeps)
    await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})
    for body in (policy.data_dir / "web_cache").rglob("*.body"):
        body.write_bytes(b'{"info": {"name": "evil", "summary": "poisoned"}}')

    result = await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})

    assert not result.from_cache and "poisoned" not in result.body.content
    assert web.paths("pypi.org") == ["/pypi/example-http/json"] * 2


async def test_non_allowlisted_host_is_blocked_before_any_request(
    tools: ToolRegistry, web: MockWeb, events_of: EventsOf
) -> None:
    with pytest.raises(OperationBlocked) as blocked:
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": "https://evil.example.net/page"})

    assert blocked.value.reason is BlockReason.POLICY_VIOLATION
    assert "not allowlisted" in blocked.value.detail
    assert web.requests == []
    assert len(await events_of("security.operation_blocked")) == 1


async def test_general_http_allowlist_does_not_open_web_research(
    tools: ToolRegistry, web: MockWeb
) -> None:
    # api.example.com is on the http_request allowlist (see make_policy), not the web one.
    with pytest.raises(OperationBlocked, match="not allowlisted"):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": "https://api.example.com/x"})
    assert web.requests == []


async def test_redirect_off_the_allowlist_is_refused(tools: ToolRegistry, web: MockWeb) -> None:
    with pytest.raises(OperationBlocked, match="redirect refused"):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": f"https://{DOCS_HOST}/moved"})
    assert all(logical_host(request) == DOCS_HOST for request in web.requests)


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
async def test_allowlisted_name_resolving_to_private_address_is_blocked(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
    address: str,
) -> None:
    def resolve(host: str, port: int) -> list:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, port, 0, 0) if ":" in address else (address, port)
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    tools, _ = build(permissions, database, recorder, web, sleeps, resolver=resolve)

    with pytest.raises(OperationBlocked, match="non-public"):
        await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})
    # Page tools fail closed too: robots.txt cannot be fetched, so the page is not fetched.
    with pytest.raises(OperationBlocked, match="robots.txt fetch blocked"):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})
    assert web.requests == []


async def test_unacceptable_content_type_is_refused_before_reading(
    tools: ToolRegistry, web: MockWeb
) -> None:
    with pytest.raises(OperationBlocked, match="content type 'application/zip'"):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": f"https://{DOCS_HOST}/download.bin"})


async def test_page_size_cap_truncates(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
) -> None:
    tools, _ = build(permissions, database, recorder, web, sleeps, max_page_bytes=1024)

    result = await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})

    assert result.truncated and len(result.body.content.encode()) <= 1024


async def test_injected_instructions_stay_flagged_data(
    tools: ToolRegistry,
    web: MockWeb,
    permissions: PermissionService,
    events_of: EventsOf,
) -> None:
    policy_before = repr(permissions.policy)

    result = await tools.invoke(
        AgentId.SCOUT, "web.extract", {"url": f"https://{DOCS_HOST}/injection.html"}
    )

    assert {"approval_claim", "ignore_instructions", "pipe_to_shell", "credential_request"} <= set(
        result.suspicious_patterns
    )
    assert "evil.example.net" in result.body.content
    # The page's link and commands were never followed or executed.
    assert all(logical_host(request) == DOCS_HOST for request in web.requests)
    assert web.paths() == ["/robots.txt", "/injection.html"]
    assert len(await events_of("security.operation_authorized")) == 2
    assert repr(permissions.policy) == policy_before
    assert "<<untrusted source=" in result.body.quote()


async def test_web_links_crawl_is_bounded_same_host_and_robots_aware(
    tools: ToolRegistry, web: MockWeb
) -> None:
    result = await tools.invoke(
        AgentId.SCOUT,
        "web.links",
        {"url": f"https://{DOCS_HOST}/guide/index.html", "depth": 1, "max_pages": 3},
    )

    fetched = [page.url for page in result.pages]
    assert fetched == [
        f"https://{DOCS_HOST}/guide/index.html",
        f"https://{DOCS_HOST}/guide/article.html",
        f"https://{DOCS_HOST}/guide/timeouts.html",
    ]
    assert all(logical_host(request) == DOCS_HOST for request in web.requests)
    urls = {link.url for link in result.links}
    assert "https://other.example.com/elsewhere" in urls
    assert f"https://{DOCS_HOST}/guide/article.html" in urls
    assert f"https://{DOCS_HOST}/guide/article.html#again" not in urls
    assert result.notes and "crawl stopped" in result.notes[0]

    wider = await tools.invoke(
        AgentId.SCOUT,
        "web.links",
        {"url": f"https://{DOCS_HOST}/guide/index.html", "depth": 1, "max_pages": 10},
    )
    private = [page for page in wider.pages if "/private/" in page.url]
    assert len(private) == 1 and private[0].note.startswith("blocked: robots.txt disallows")
    assert "/private/internal.html" not in web.paths()
    assert not wider.notes and len(wider.pages) == 5


async def test_web_links_input_limits_are_validated(tools: ToolRegistry) -> None:
    with pytest.raises(ToolInputError):
        await tools.invoke(AgentId.SCOUT, "web.links", {"url": ARTICLE, "depth": 4})
    with pytest.raises(ToolInputError):
        await tools.invoke(AgentId.SCOUT, "web.links", {"url": ARTICLE, "max_pages": 21})
    with pytest.raises(ToolInputError):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": "file:///etc/passwd"})
    with pytest.raises(ToolInputError):
        await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": "https://user:pw@pypi.org/"})


async def test_pypi_package_facts_without_robots_check(tools: ToolRegistry, web: MockWeb) -> None:
    result = await tools.invoke(AgentId.ROGUE, "pypi.package", {"name": "example-http"})

    assert result.facts["version"] == "1.4.2"
    assert result.facts["license"] == "BSD-3-Clause"
    assert result.facts["release_count"] == 4
    assert result.facts["latest_upload"].startswith("2026-03-01T10:00")
    assert "PyPI package: example-http 1.4.2" in result.body.content
    assert result.body.source == "https://pypi.org/pypi/example-http/json"
    assert web.paths() == ["/pypi/example-http/json"]


async def test_pypi_unknown_package_reports_status(tools: ToolRegistry) -> None:
    result = await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "no-such-pkg"})
    assert result.status_code == 404 and result.facts == {}
    with pytest.raises(ToolInputError):
        await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "../../admin"})


async def test_github_repo_includes_latest_release(tools: ToolRegistry, web: MockWeb) -> None:
    result = await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})

    assert result.facts["stars"] == 1234 and result.facts["license"] == "BSD-3-Clause"
    assert result.facts["latest_release"]["tag"] == "1.4.2"
    assert "Latest release: 1.4.2" in result.body.content
    assert web.paths() == ["/repos/example/http", "/repos/example/http/releases/latest"]
    assert web.requests[0].headers["accept"] == "application/vnd.github+json"
    assert "authorization" not in web.requests[0].headers


async def test_github_rate_limit_exhaustion_stops_further_calls(
    tools: ToolRegistry, web: MockWeb
) -> None:
    web.github_remaining = 1
    await tools.invoke(AgentId.ROGUE, "github.search", {"query": "http client"})

    with pytest.raises(RateLimited, match="rate limit exhausted"):
        await tools.invoke(AgentId.ROGUE, "github.search", {"query": "streaming"})
    assert web.paths() == ["/search/repositories"]


async def test_github_search_lists_repositories(tools: ToolRegistry, web: MockWeb) -> None:
    result = await tools.invoke(
        AgentId.ROGUE, "github.search", {"query": "http  client", "limit": 2}
    )

    assert [hit["full_name"] for hit in result.facts["repositories"]] == [
        "example/http",
        "other/streamer",
    ]
    assert dict(web.requests[0].url.params) == {"q": "http client", "per_page": "2"}


async def test_docs_search_uses_sphinx_inventory(tools: ToolRegistry, web: MockWeb) -> None:
    result = await tools.invoke(
        AgentId.SCOUT,
        "docs.search",
        {"base_url": f"https://{DOCS_HOST}/en/latest", "query": "stream"},
    )

    assert result.hits and result.hits[0].url.startswith(f"https://{DOCS_HOST}/en/latest/")
    titles = [hit.title for hit in result.hits]
    assert "example_http.Client.stream" in titles
    assert any(hit.url.endswith("api.html#example_http.Client.stream") for hit in result.hits)
    assert "via objects.inv" in result.body.content
    assert web.paths() == ["/robots.txt", "/en/latest/objects.inv"]


async def test_docs_search_falls_back_to_legacy_searchindex(
    tools: ToolRegistry, web: MockWeb
) -> None:
    result = await tools.invoke(
        AgentId.SCOUT, "docs.search", {"base_url": f"https://{DOCS_HOST}/old/", "query": "stream"}
    )

    assert [hit.title for hit in result.hits] == ["Streaming large downloads"]
    assert result.hits[0].url == f"https://{DOCS_HOST}/old/guide/streaming.html"
    assert result.notes == ("objects.inv returned HTTP 404",)
    assert web.paths() == ["/robots.txt", "/old/objects.inv", "/old/searchindex.js"]


async def test_docs_search_only_targets_configured_docs_hosts(
    tools: ToolRegistry, web: MockWeb
) -> None:
    with pytest.raises(ToolInputError, match="documentation host"):
        await tools.invoke(
            AgentId.SCOUT, "docs.search", {"base_url": "https://pypi.org/", "query": "x"}
        )
    result = await tools.invoke(
        AgentId.SCOUT,
        "docs.search",
        {"base_url": "https://example.readthedocs.io/en/latest/", "query": "timeouts"},
    )
    assert result.hits[0].title == "Connect timeouts"
    assert logical_host(web.requests[0]) == "example.readthedocs.io"


async def test_only_research_agents_may_fetch(tools: ToolRegistry, web: MockWeb) -> None:
    with pytest.raises(OperationBlocked) as blocked:
        await tools.invoke(AgentId.FORGE, "web.fetch", {"url": ARTICLE})
    assert blocked.value.reason is BlockReason.AGENT_NOT_PERMITTED
    assert web.requests == []


async def test_forged_arguments_do_not_reach_the_network(
    permissions: PermissionService, tools: ToolRegistry, web: MockWeb
) -> None:
    operation = tools.build_operation(AgentId.SCOUT, "web.fetch", {"url": ARTICLE})
    forged = operation.model_copy(
        update={"arguments": {"tool": "web.fetch", "url": "https://pypi.org/x"}}
    )
    grant = await permissions.authorize(forged)

    with pytest.raises(OperationBlocked, match="does not match"):
        await tools.get("web.fetch").adapter.execute(forged, grant)
    assert web.requests == []


async def test_private_resolution_for_one_host_does_not_affect_others(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    sleeps: list[float],
) -> None:
    tools, _ = build(
        permissions,
        database,
        recorder,
        web,
        sleeps,
        resolver=resolver_mapping({"api.github.com": "192.168.1.10"}),
    )
    with pytest.raises(OperationBlocked):
        await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})
    result = await tools.invoke(AgentId.ROGUE, "pypi.package", {"name": "example-http"})
    assert result.status_code == 200
