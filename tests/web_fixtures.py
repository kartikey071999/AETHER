"""Offline web research fixtures: a mock internet behind httpx.MockTransport.

Every response comes from ``tests/fixtures/web`` or is generated here; nothing touches the
network. The resolver pins every host to a public documentation address unless a test maps it
to a private one.
"""

import json
import socket
import zlib
from collections.abc import Callable
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "web"
DOCS_HOST = "docs.example.org"
WEB_HOSTS = (DOCS_HOST, "pypi.org", "api.github.com", "*.readthedocs.io")
PUBLIC_IP = "93.184.216.34"
RESET_EPOCH = 4_102_444_800  # 2100-01-01: far future, capped by the client


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def objects_inv(entries: list[str], project: str = "Example HTTP") -> bytes:
    header = (
        f"# Sphinx inventory version 2\n# Project: {project}\n# Version: 1.4\n"
        "# The remainder of this file is compressed using zlib.\n"
    ).encode()
    return header + zlib.compress("\n".join(entries).encode() + b"\n")


INVENTORY = objects_inv(
    [
        "example_http.Client.stream py:method 1 api.html#$ -",
        "example_http.Response.iter_bytes py:method 1 api.html#$ -",
        "example_http.Timeout py:class 1 api.html#$ -",
        "streaming std:doc -1 guide/article.html Streaming responses",
        "timeouts std:label -1 guide/timeouts.html#connect Connect timeouts",
    ]
)
# Old Sphinx emitted JS object literals with bare keys (not valid JSON).
SEARCHINDEX = (
    'Search.setIndex({docnames:["index","guide/streaming","guide/proxies"],'
    'filenames:["index.rst","guide/streaming.rst","guide/proxies.rst"],'
    'titles:["Home","Streaming large downloads","Proxy configuration"],'
    "terms:{stream:[1],download:1,proxi:2,large:[1]},"
    "titleterms:{stream:1,proxi:2,download:1}})"
)


def public_resolver(host: str, port: int) -> list:
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (PUBLIC_IP, port))]


def resolver_mapping(addresses: dict[str, str]) -> Callable[[str, int], list]:
    def resolve(host: str, port: int) -> list:
        address = addresses.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    return resolve


def logical_host(request: httpx.Request) -> str:
    """The requested host name; the URL itself carries the pinned IP address."""
    return request.headers.get("host", request.url.host).rsplit(":", 1)[0]


class MockWeb:
    """Routes requests by host and path; records every request it serves."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.github_remaining = 59
        self.overrides: dict[tuple[str, str], httpx.Response] = {}

    def paths(self, host: str | None = None) -> list[str]:
        return [
            request.url.path
            for request in self.requests
            if host is None or logical_host(request) == host
        ]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = logical_host(request), request.url.path
        if (host, path) in self.overrides:
            return self.overrides[(host, path)]
        if host == DOCS_HOST or host.endswith(".readthedocs.io"):
            return self._docs(path)
        if host == "pypi.org":
            return self._pypi(path)
        if host == "api.github.com":
            return self._github(path)
        return httpx.Response(404, text="not found")

    def _docs(self, path: str) -> httpx.Response:
        html = {"content-type": "text/html; charset=utf-8"}
        pages = {
            "/guide/article.html": "article.html",
            "/guide/index.html": "index.html",
            "/injection.html": "injection.html",
        }
        if path == "/robots.txt":
            return httpx.Response(
                200, content=fixture_bytes("robots.txt"), headers={"content-type": "text/plain"}
            )
        if path in pages:
            return httpx.Response(200, content=fixture_bytes(pages[path]), headers=html)
        if path in {"/guide/timeouts.html", "/guide/proxies.html", "/private/internal.html"}:
            title = path.rsplit("/", 1)[-1]
            body = f"<html><head><title>{title}</title></head><body><p>{title}</p></body></html>"
            return httpx.Response(200, text=body, headers=html)
        if path == "/en/latest/objects.inv":
            return httpx.Response(
                200, content=INVENTORY, headers={"content-type": "application/octet-stream"}
            )
        if path == "/old/searchindex.js":
            return httpx.Response(
                200, text=SEARCHINDEX, headers={"content-type": "application/javascript"}
            )
        if path == "/download.bin":
            return httpx.Response(
                200, content=b"\x00\x01binary", headers={"content-type": "application/zip"}
            )
        if path == "/moved":
            return httpx.Response(302, headers={"location": "https://evil.example.net/landing"})
        return httpx.Response(404, text="not found", headers=html)

    def _pypi(self, path: str) -> httpx.Response:
        if path == "/pypi/example-http/json":
            return httpx.Response(
                200,
                content=fixture_bytes("pypi_example_http.json"),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    def _github(self, path: str) -> httpx.Response:
        self.github_remaining = max(self.github_remaining - 1, 0)
        headers = {
            "content-type": "application/json; charset=utf-8",
            "x-ratelimit-remaining": str(self.github_remaining),
            "x-ratelimit-reset": str(RESET_EPOCH),
        }
        if path == "/repos/example/http":
            return httpx.Response(200, content=fixture_bytes("github_repo.json"), headers=headers)
        if path == "/repos/example/http/releases/latest":
            release = {"tag_name": "1.4.2", "name": "1.4.2", "published_at": "2026-03-01T10:00:00Z"}
            return httpx.Response(200, content=json.dumps(release).encode(), headers=headers)
        if path == "/search/repositories":
            return httpx.Response(200, content=fixture_bytes("github_search.json"), headers=headers)
        return httpx.Response(404, json={"message": "Not Found"}, headers=headers)
