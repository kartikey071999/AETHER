"""The optional GitHub token reaches api.github.com only, and never leaves a trace."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from web_fixtures import DOCS_HOST, WEB_HOSTS, MockWeb, logical_host, public_resolver

from aether.communication.messages import AgentId
from aether.main import main
from aether.observability.recorder import EventRecorder
from aether.security import PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import EventRecord, ExecutionRunRecord
from aether.tools import ToolRegistry
from aether.tools.web import (
    GitHubStatus,
    RateBucket,
    SafeWeb,
    WebSettings,
    fetch_github_status,
    github_auth_headers,
    register_web_tools,
)
from aether.tools.web.github import GITHUB_RATE_LIMIT_URL
from aether.tools.web.politeness import GitHubRateLimit

TOKEN = "github_pat_FAKE0123456789_abcdefghijklmnopqrstuvwxyz"
SEARCH_URL = "https://api.github.com/search/repositories?q=x"


@pytest.fixture
def policy(policy_factory: Callable[..., SecurityPolicy]) -> SecurityPolicy:
    return policy_factory(max_response_bytes=1024 * 1024, web_allowed_hosts=WEB_HOSTS)


@pytest.fixture
def web() -> MockWeb:
    return MockWeb()


def build(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    token: str | None,
) -> ToolRegistry:
    async def no_sleep(seconds: float) -> None:
        return None

    options: dict[str, object] = {
        "allowed_hosts": list(WEB_HOSTS),
        "docs_hosts": [DOCS_HOST],
        "github_token": token,
    }
    adapter = SafeWeb(
        permissions,
        database.sessions,
        recorder,
        WebSettings(_env_file=None, **options),
        transport=web.transport,
        resolver=public_resolver,
        monotonic=lambda: 0.0,
        sleep=no_sleep,
    )
    registry = ToolRegistry(permissions)
    register_web_tools(registry, adapter)
    return registry


def authorization(web: MockWeb, host: str) -> list[str | None]:
    return [
        request.headers.get("authorization")
        for request in web.requests
        if logical_host(request) == host
    ]


def test_token_setting_defaults_to_none_and_hides_its_value() -> None:
    assert WebSettings(_env_file=None).github_token is None
    assert WebSettings(_env_file=None, github_token="  ").github_token is None
    settings = WebSettings(_env_file=None, github_token=f" {TOKEN} ")
    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == TOKEN
    assert TOKEN not in repr(settings) and TOKEN not in str(settings.model_dump())


def test_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_WEB_GITHUB_TOKEN", TOKEN)
    settings = WebSettings(_env_file=None)
    assert settings.github_token is not None
    assert settings.github_token.get_secret_value() == TOKEN


def test_malformed_token_is_rejected_without_echoing_it() -> None:
    with pytest.raises(ValidationError) as error:
        WebSettings(_env_file=None, github_token="github_pat_has space")
    assert "github_pat_has" not in str(error.value)


def test_header_is_built_only_for_https_api_github_com() -> None:
    settings = WebSettings(_env_file=None, github_token=TOKEN)
    token = settings.github_token
    assert github_auth_headers(SEARCH_URL, token) == {"Authorization": f"Bearer {TOKEN}"}
    assert github_auth_headers("https://API.GitHub.com/rate_limit", token)
    for url in (
        "http://api.github.com/rate_limit",
        "https://github.com/example/http",
        "https://api.github.com.evil.example/rate_limit",
        "https://evil.example/api.github.com",
        "https://uploads.github.com/x",
        "https://pypi.org/pypi/x/json",
        f"https://{DOCS_HOST}/guide/article.html",
    ):
        assert github_auth_headers(url, token) == {}
    assert github_auth_headers(SEARCH_URL, None) == {}


async def test_token_is_sent_only_to_api_github_com(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
) -> None:
    tools = build(permissions, database, recorder, web, TOKEN)

    await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})
    await tools.invoke(AgentId.ROGUE, "github.search", {"query": "http client"})
    await tools.invoke(AgentId.SCOUT, "pypi.package", {"name": "example-http"})
    await tools.invoke(AgentId.SCOUT, "web.fetch", {"url": f"https://{DOCS_HOST}/guide/index.html"})

    assert authorization(web, "api.github.com") == [f"Bearer {TOKEN}"] * 3
    assert authorization(web, "pypi.org") == [None]
    assert authorization(web, DOCS_HOST) == [None, None]


async def test_unset_token_sends_no_authorization(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
) -> None:
    tools = build(permissions, database, recorder, web, None)

    await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})

    assert web.requests and all("authorization" not in r.headers for r in web.requests)


@pytest.mark.parametrize(
    "target", ["https://pypi.org/pypi/example-http/json", "https://docs.example.org/x"]
)
async def test_token_is_dropped_when_api_github_com_redirects_elsewhere(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    target: str,
) -> None:
    web.overrides[("api.github.com", "/repos/example/moved")] = httpx.Response(
        302, headers={"location": target}
    )
    tools = build(permissions, database, recorder, web, TOKEN)

    await tools.invoke(
        AgentId.SCOUT, "web.fetch", {"url": "https://api.github.com/repos/example/moved"}
    )

    followed = [r for r in web.requests if logical_host(r) != "api.github.com"]
    assert followed and all("authorization" not in r.headers for r in followed)
    assert authorization(web, "api.github.com")[-1] == f"Bearer {TOKEN}"


async def test_token_is_kept_on_a_same_origin_redirect(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
) -> None:
    web.overrides[("api.github.com", "/repos/example/moved")] = httpx.Response(
        302, headers={"location": "/repos/example/http"}
    )
    tools = build(permissions, database, recorder, web, TOKEN)

    await tools.invoke(
        AgentId.SCOUT, "web.fetch", {"url": "https://api.github.com/repos/example/moved"}
    )

    assert "/repos/example/http" in web.paths("api.github.com")
    assert set(authorization(web, "api.github.com")) == {f"Bearer {TOKEN}"}


async def test_token_never_appears_in_records_logs_cache_or_errors(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
    web: MockWeb,
    tmp_path: Path,
    policy: SecurityPolicy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tools = build(permissions, database, recorder, web, TOKEN)
    await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})
    await tools.invoke(AgentId.ROGUE, "github.search", {"query": "http client"})
    # Cached replay and an upstream failure both stay clean.
    await tools.invoke(AgentId.ROGUE, "github.search", {"query": "http client"})
    web.overrides[("api.github.com", "/repos/example/down")] = httpx.Response(
        500, text=f"echo {TOKEN}"
    )
    await tools.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "down"})

    async with database.sessions() as session:
        runs = list(await session.scalars(select(ExecutionRunRecord)))
        events = list(await session.scalars(select(EventRecord)))
    assert runs and events
    dumped = repr([vars(row) for row in runs]) + repr([vars(row) for row in events])
    assert TOKEN not in dumped and "Bearer" not in dumped
    assert TOKEN not in caplog.text
    cache_files = list((policy.data_dir / "web_cache").rglob("*"))
    assert any(path.is_file() for path in cache_files)
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "security.db":
            assert TOKEN.encode() not in path.read_bytes(), path
    assert TOKEN.encode() not in (tmp_path / "security.db").read_bytes()


async def test_transport_error_message_does_not_carry_the_token(
    permissions: PermissionService,
    database: Database,
    recorder: EventRecorder,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {request.headers['authorization']}")

    adapter = SafeWeb(
        permissions,
        database.sessions,
        recorder,
        WebSettings(_env_file=None, allowed_hosts=list(WEB_HOSTS), github_token=TOKEN),
        transport=httpx.MockTransport(fail),
        resolver=public_resolver,
    )
    registry = ToolRegistry(permissions)
    register_web_tools(registry, adapter)

    with pytest.raises(Exception) as error:
        await registry.invoke(AgentId.ROGUE, "github.repo", {"owner": "example", "repo": "http"})

    assert TOKEN not in str(error.value) and TOKEN not in repr(error.value)


# -- rate-limit headers -------------------------------------------------------------------------
def test_exhausted_quota_blocks_until_the_advertised_reset() -> None:
    now = [1000.0]
    limits = GitHubRateLimit(clock=lambda: now[0])
    limits.observe("core", 200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1090"})
    assert limits.blocked_for("core") == 90
    assert limits.blocked_for("search") == 0
    now[0] = 1090.0
    assert limits.blocked_for("core") == 0


def test_secondary_limit_without_reset_backs_off_a_minute() -> None:
    limits = GitHubRateLimit(clock=lambda: 1000.0)
    limits.observe("core", 429, {})
    assert limits.blocked_for("core") == 60
    limits.observe("search", 403, {"retry-after": "7"})
    assert limits.blocked_for("search") == 7


def test_remaining_quota_does_not_block() -> None:
    limits = GitHubRateLimit(clock=lambda: 1000.0)
    limits.observe("core", 200, {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "5000"})
    assert limits.blocked_for("core") == 0


# -- aether web status --------------------------------------------------------------------------
def _rate_limit_body() -> dict[str, object]:
    return {
        "resources": {
            "core": {"limit": 5000, "remaining": 4990, "reset": 4_102_444_800, "used": 10},
            "search": {"limit": 30, "remaining": 29, "reset": 4_102_444_800, "used": 1},
        }
    }


async def test_status_reads_rate_limit_and_sends_token_to_the_api_host() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_rate_limit_body())

    status = await fetch_github_status(
        WebSettings(_env_file=None, github_token=TOKEN), transport=httpx.MockTransport(handle)
    )

    assert str(seen[0].url) == GITHUB_RATE_LIMIT_URL
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert status.token_configured and status.reachable
    assert status.buckets is not None
    assert status.buckets["core"] == RateBucket(5000, 4990, datetime(2100, 1, 1, tzinfo=UTC))
    assert TOKEN not in repr(status)


async def test_status_without_token_is_unauthenticated_and_tolerates_failures() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(403, json={"message": "rate limited"})

    status = await fetch_github_status(
        WebSettings(_env_file=None), transport=httpx.MockTransport(handle)
    )
    assert "authorization" not in seen[0].headers
    assert status == GitHubStatus(False, True, 403)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route {request.headers.get('authorization')}")

    status = await fetch_github_status(
        WebSettings(_env_file=None, github_token=TOKEN), transport=httpx.MockTransport(down)
    )
    assert status == GitHubStatus(True, False, error="ConnectError")


def test_web_status_command_prints_yes_no_and_never_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake(settings: WebSettings) -> GitHubStatus:
        return GitHubStatus(
            settings.github_token is not None,
            True,
            200,
            {"core": RateBucket(5000, 4990, datetime(2100, 1, 1, tzinfo=UTC))},
        )

    monkeypatch.setattr("aether.tools.web.fetch_github_status", fake)
    monkeypatch.setenv("AETHER_WEB_GITHUB_TOKEN", TOKEN)
    monkeypatch.chdir(Path(__file__).parent)  # no repository .env is read

    main(["web", "status"])

    output = capsys.readouterr().out
    assert "GitHub token configured: yes" in output
    assert "core rate limit: 4990/5000 remaining" in output
    assert TOKEN not in output

    monkeypatch.delenv("AETHER_WEB_GITHUB_TOKEN")
    main(["web", "status"])
    assert "GitHub token configured: no" in capsys.readouterr().out


def test_web_status_command_fails_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake(settings: WebSettings) -> GitHubStatus:
        return GitHubStatus(False, False, error="ConnectError")

    monkeypatch.setattr("aether.tools.web.fetch_github_status", fake)
    monkeypatch.delenv("AETHER_WEB_GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(Path(__file__).parent)

    with pytest.raises(SystemExit) as exit_info:
        main(["web", "status"])

    assert exit_info.value.code == 1
    assert "unreachable (ConnectError)" in capsys.readouterr().err
