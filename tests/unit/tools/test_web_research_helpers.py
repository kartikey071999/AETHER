"""Pure web research helpers: extraction, robots rules, politeness, cache and source parsers."""

import json
import zlib
from pathlib import Path

import pytest
from web_fixtures import INVENTORY, SEARCHINDEX, fixture_bytes, objects_inv

from aether.tools.web.cache import WebCache
from aether.tools.web.extract import extract_html, normalize_url
from aether.tools.web.politeness import GitHubRateLimit, HostRateLimiter
from aether.tools.web.robots import RobotsCache, parse_robots, rules_for_response
from aether.tools.web.settings import DEFAULT_WEB_ALLOWED_HOSTS, WebSettings
from aether.tools.web.sources import (
    SourceFormatError,
    parse_objects_inv,
    parse_searchindex,
    search_inventory,
    search_searchindex,
    summarize_github_repo,
    summarize_pypi,
)

UA = "AETHER-research/0.1.0 (+test)"


# -- extraction ---------------------------------------------------------------------------------
def test_extract_prefers_main_content_and_drops_hidden_and_boilerplate() -> None:
    page = extract_html(
        fixture_bytes("article.html").decode(), "https://docs.example.org/guide/article.html"
    )
    assert page.text.startswith("# Streaming responses")
    assert "Guide home" not in page.text and "FOOTER" not in page.text
    assert page.headings == ("Streaming responses", "Iterating bytes")
    assert page.metadata["lang"] == "en"
    assert not page.truncated and not page.nofollow


def test_extract_without_main_uses_body_and_truncates() -> None:
    html = "<html><body><p>" + "word " * 200 + "</p><p>tail</p></body></html>"
    page = extract_html(html, "https://example.org/", max_chars=300)
    assert page.truncated and page.text.endswith("[truncated]")
    assert len(page.text) <= 300 + len("\n[truncated]")


def test_extract_handles_nested_hidden_and_malformed_markup() -> None:
    html = (
        "<div hidden><div>inner hidden</div><p>still hidden</p></div><p>visible"
        "<script>evil()</script> text<style>x{}</style><svg><text>svg text</text></svg>"
        "<template><p>template</p></template>"
    )
    page = extract_html(html, "https://example.org/")
    assert page.text == "visible text"


def test_extract_respects_base_and_nofollow_and_link_cap() -> None:
    links = "".join(f'<a href="p{i}.html">p{i}</a>' for i in range(10))
    html = (
        '<head><base href="https://example.org/docs/"><meta name="robots" content="NOFOLLOW">'
        f"</head><body>{links}</body>"
    )
    page = extract_html(html, "https://example.org/", max_links=3)
    assert page.nofollow
    assert [link.url for link in page.links] == [
        "https://example.org/docs/p0.html",
        "https://example.org/docs/p1.html",
        "https://example.org/docs/p2.html",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.org/a#b", "https://example.org/a"),
        ("http://example.org", "http://example.org"),
        ("ftp://example.org/x", None),
        ("javascript:alert(1)", None),
        ("https://user:pw@example.org/", None),
        ("https:///nohost", None),
    ],
)
def test_normalize_url(url: str, expected: str | None) -> None:
    assert normalize_url(url) == expected


# -- robots.txt ---------------------------------------------------------------------------------
def test_robots_rules_and_crawl_delay() -> None:
    rules = parse_robots(fixture_bytes("robots.txt").decode())
    assert rules.allows("https://docs.example.org/guide/a.html", UA)
    assert not rules.allows("https://docs.example.org/private/x.html", UA)
    assert not rules.allows("https://docs.example.org/guide/a.html", "BadBot/1.0")
    assert rules.crawl_delay(UA) == 2.0


@pytest.mark.parametrize(
    ("status", "content_type", "allowed"),
    [
        (404, "text/html", True),
        (401, "text/plain", True),
        (500, "text/plain", False),
        (503, "text/plain", False),
        (200, "text/html", True),
        (304, "text/plain", False),
    ],
)
def test_robots_status_handling_follows_rfc_9309(
    status: int, content_type: str, allowed: bool
) -> None:
    rules = rules_for_response(status, content_type, "User-agent: *\nDisallow: /")
    assert rules.allows("https://example.org/page", UA) is allowed


def test_robots_cache_expires() -> None:
    now = [0.0]
    cache = RobotsCache(60, clock=lambda: now[0])
    cache.put("https://example.org", parse_robots(""))
    assert cache.get("https://example.org") is not None
    now[0] = 61
    assert cache.get("https://example.org") is None


# -- politeness ---------------------------------------------------------------------------------
async def test_host_rate_limiter_spaces_each_host_independently() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = HostRateLimiter(1.0, clock=lambda: now[0], sleep=sleep)
    assert await limiter.wait("a.example") == 0
    assert await limiter.wait("b.example") == 0
    assert await limiter.wait("a.example") == 1.0
    limiter.set_crawl_delay("a.example", 120)
    assert limiter.interval_for("a.example") == 30.0  # crawl delays are capped
    now[0] += 5
    assert await limiter.wait("b.example") == 0
    assert sleeps == [1.0]


def test_github_rate_limit_blocks_until_reset_with_cap() -> None:
    now = [1_000.0]
    limits = GitHubRateLimit(clock=lambda: now[0])
    limits.observe("core", 200, {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": "2000"})
    assert limits.blocked_for("core") == 0
    limits.observe("core", 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"})
    assert limits.blocked_for("core") == 100 and limits.blocked_for("search") == 0
    limits.observe("search", 429, {"Retry-After": "30"})
    assert limits.blocked_for("search") == 30
    limits.observe("core", 200, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999999"})
    assert limits.blocked_for("core") == 3600
    now[0] += 3601
    assert limits.blocked_for("core") == 0


# -- cache --------------------------------------------------------------------------------------
def test_cache_round_trip_ttl_and_integrity(tmp_path: Path) -> None:
    now = [0.0]
    cache = WebCache(tmp_path, 10, clock=lambda: now[0])
    url = "https://example.org/a"
    cache.put(
        "page",
        url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        body=b"<p>x</p>",
        truncated=False,
    )
    hit = cache.get("page", url)
    assert hit is not None and hit.body == b"<p>x</p>"
    assert cache.get("api", url) is None
    next(tmp_path.rglob("*.body")).write_bytes(b"tampered")
    assert cache.get("page", url) is None
    cache.put(
        "page", url, final_url=url, status_code=200, content_type="", body=b"y", truncated=False
    )
    now[0] = 11
    assert cache.get("page", url) is None


def test_cache_disabled_with_zero_ttl(tmp_path: Path) -> None:
    cache = WebCache(tmp_path, 0)
    cache.put(
        "page",
        "https://e.org",
        final_url="",
        status_code=200,
        content_type="",
        body=b"",
        truncated=False,
    )
    assert cache.get("page", "https://e.org") is None and not any(tmp_path.iterdir())


# -- sources ------------------------------------------------------------------------------------
def test_objects_inv_parse_and_search() -> None:
    entries = parse_objects_inv(INVENTORY, "https://docs.example.org/en/latest/")
    assert (
        "example_http.Timeout",
        "py:class",
        "example_http.Timeout",
        "https://docs.example.org/en/latest/api.html#example_http.Timeout",
    ) in entries
    hits = search_inventory(entries, "iter bytes", 3)
    assert hits[0].title == "example_http.Response.iter_bytes"
    assert search_inventory(entries, "   ", 3) == []


def test_objects_inv_decompression_is_bounded() -> None:
    header = objects_inv([]).split(b"zlib.\n")[0] + b"zlib.\n"
    entry = b"pkg.thing py:class 1 api.html#$ -\n"
    bomb = header + zlib.compress(entry * 200_000)
    bounded = parse_objects_inv(bomb, "https://e.org/", max_bytes=len(entry) * 10)
    assert 9 <= len(bounded) <= 10
    with pytest.raises(ValueError, match="unsupported"):
        parse_objects_inv(b"<html>not an inventory</html>\n\n\n\n", "https://e.org/")


def test_searchindex_accepts_json_and_legacy_js_literals() -> None:
    legacy = parse_searchindex(SEARCHINDEX)
    modern = parse_searchindex(f"Search.setIndex({json.dumps(legacy)})")
    assert legacy == modern
    hits = search_searchindex(legacy, "https://docs.example.org/", "large download", 5)
    assert hits[0].title == "Streaming large downloads" and hits[0].score == 1.0
    assert hits[0].url == "https://docs.example.org/guide/streaming.html"
    assert search_searchindex(legacy, "https://docs.example.org/", "nothing", 5) == []
    with pytest.raises(ValueError):
        parse_searchindex("alert('not an index')")


def test_pypi_and_github_summaries_are_bounded_and_strict() -> None:
    facts = summarize_pypi(json.loads(fixture_bytes("pypi_example_http.json")))
    assert facts["requires_dist"][0] == "anyio>=4"
    huge = {"info": {"name": "x", "summary": "s" * 10_000, "classifiers": ["c"] * 500}}
    bounded = summarize_pypi(huge)
    assert len(bounded["summary"]) == 500 and len(bounded["classifiers"]) == 20
    with pytest.raises(SourceFormatError):
        summarize_pypi(["not", "a", "document"])
    repo = summarize_github_repo(json.loads(fixture_bytes("github_repo.json")))
    assert repo["topics"] == ["http", "client", "asyncio"]
    assert summarize_github_repo({"full_name": "a/b", "stargazers_count": True})["stars"] is None


# -- settings -----------------------------------------------------------------------------------
def test_web_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = WebSettings(_env_file=None)
    assert set(defaults.allowed_hosts) == set(DEFAULT_WEB_ALLOWED_HOSTS)
    assert set(defaults.docs_hosts) <= set(defaults.allowed_hosts)
    monkeypatch.setenv("AETHER_WEB_ALLOWED_HOSTS", '["Docs.Example.org.", "pypi.org"]')
    monkeypatch.setenv("AETHER_WEB_PER_HOST_INTERVAL_SECONDS", "2.5")
    configured = WebSettings(_env_file=None)
    assert configured.allowed_hosts == ["docs.example.org", "pypi.org"]
    assert configured.per_host_interval_seconds == 2.5
    with pytest.raises(ValueError):
        WebSettings(_env_file=None, user_agent="bad\r\nX-Injected: 1")
