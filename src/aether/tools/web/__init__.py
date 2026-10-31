"""Web research tools: allowlisted, robots-aware, rate-limited, cached, untrusted output."""

from aether.security.operations import OperationKind
from aether.tools.registry import Tool, ToolRegistry
from aether.tools.web.adapter import (
    WEB_INPUTS,
    DocsSearchInput,
    GithubRepoInput,
    GithubSearchInput,
    PageRecord,
    PypiPackageInput,
    RateLimited,
    SafeWeb,
    WebExtractInput,
    WebFetchInput,
    WebLinksInput,
    WebResult,
)
from aether.tools.web.extract import ExtractedPage, Link, extract_html
from aether.tools.web.settings import DEFAULT_WEB_ALLOWED_HOSTS, WebSettings
from aether.tools.web.sources import DocHit

WEB_TOOL_DESCRIPTIONS = {
    "web.fetch": "GET an allowlisted page (robots.txt honoured); returns untrusted text.",
    "web.extract": "Readable text, title, headings and links of an allowlisted page.",
    "web.links": "Links on an allowlisted page, optionally a bounded same-host crawl.",
    "pypi.package": "Package facts from the PyPI JSON API (version, licence, releases).",
    "github.repo": "Repository facts from the GitHub REST API (stars, licence, activity).",
    "github.search": "Search GitHub repositories (unauthenticated REST API).",
    "docs.search": "Search a Sphinx documentation site via objects.inv or searchindex.js.",
}


def register_web_tools(registry: ToolRegistry, adapter: SafeWeb) -> None:
    for name, model in WEB_INPUTS.items():
        registry.register(
            Tool(name, OperationKind.WEB_FETCH, model, adapter, WEB_TOOL_DESCRIPTIONS[name])
        )


__all__ = [
    "DEFAULT_WEB_ALLOWED_HOSTS",
    "WEB_INPUTS",
    "WEB_TOOL_DESCRIPTIONS",
    "DocHit",
    "DocsSearchInput",
    "ExtractedPage",
    "GithubRepoInput",
    "GithubSearchInput",
    "Link",
    "PageRecord",
    "PypiPackageInput",
    "RateLimited",
    "SafeWeb",
    "WebExtractInput",
    "WebFetchInput",
    "WebLinksInput",
    "WebResult",
    "WebSettings",
    "extract_html",
    "register_web_tools",
]
