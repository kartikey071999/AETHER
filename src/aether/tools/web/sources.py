"""Parsers for structured research sources. Inputs are untrusted bytes; outputs are bounded.

Nothing here performs I/O: the adapter fetches through the security layer and hands the bytes
over. Every string returned is still untrusted page data.
"""

import json
import re
import zlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

_FIELD_LIMIT = 500
_LIST_LIMIT = 20
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
_TOKEN = re.compile(r"[a-z0-9_]+")


class SourceFormatError(ValueError):
    """A response did not have the structure its source documents."""


def _text(value: Any, limit: int = _FIELD_LIMIT) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -- PyPI ---------------------------------------------------------------------------------------
def summarize_pypi(document: Any) -> dict[str, Any]:
    """Key facts from the PyPI JSON API (``/pypi/<name>/json``)."""
    if not isinstance(document, dict) or not isinstance(document.get("info"), dict):
        raise SourceFormatError("not a PyPI project document")
    info: dict[str, Any] = document["info"]
    releases = document.get("releases") if isinstance(document.get("releases"), dict) else {}
    files = document.get("urls") if isinstance(document.get("urls"), list) else []
    uploaded = sorted(
        str(item.get("upload_time_iso_8601"))
        for item in files
        if isinstance(item, dict) and item.get("upload_time_iso_8601")
    )
    project_urls = info.get("project_urls") if isinstance(info.get("project_urls"), dict) else {}
    license_expression = info.get("license_expression") or info.get("license") or ""
    classifiers = [str(item) for item in info.get("classifiers") or [] if isinstance(item, str)]
    requires = [str(item) for item in info.get("requires_dist") or [] if isinstance(item, str)]
    return {
        "name": _text(info.get("name"), 200),
        "version": _text(info.get("version"), 100),
        "summary": _text(info.get("summary")),
        "license": _text(license_expression, 200),
        "requires_python": _text(info.get("requires_python"), 100),
        "author": _text(info.get("author") or info.get("author_email"), 200),
        "home_page": _text(info.get("home_page"), 300),
        "project_urls": {
            _text(name, 80): _text(url, 300)
            for name, url in list(project_urls.items())[:_LIST_LIMIT]
        },
        "release_count": len(releases),
        "latest_upload": uploaded[-1] if uploaded else "",
        "yanked": bool(info.get("yanked")),
        "requires_dist": [_text(item, 200) for item in requires[:_LIST_LIMIT]],
        "classifiers": [_text(item, 200) for item in classifiers[:_LIST_LIMIT]],
    }


def render_pypi(facts: dict[str, Any]) -> str:
    lines = [
        f"PyPI package: {facts['name']} {facts['version']}",
        f"Summary: {facts['summary']}",
        f"License: {facts['license'] or 'unknown'}",
        f"Requires-Python: {facts['requires_python'] or 'unspecified'}",
        f"Releases: {facts['release_count']}; latest upload: {facts['latest_upload'] or 'unknown'}",
    ]
    if facts["yanked"]:
        lines.append("Latest release is YANKED")
    if facts["author"]:
        lines.append(f"Author: {facts['author']}")
    for name, url in facts["project_urls"].items():
        lines.append(f"URL ({name}): {url}")
    if facts["requires_dist"]:
        lines.append("Dependencies: " + "; ".join(facts["requires_dist"]))
    return "\n".join(lines)


# -- GitHub -------------------------------------------------------------------------------------
def summarize_github_repo(document: Any) -> dict[str, Any]:
    """Key facts from ``GET /repos/{owner}/{repo}``."""
    if not isinstance(document, dict) or "full_name" not in document:
        raise SourceFormatError("not a GitHub repository document")
    license_info = document.get("license") if isinstance(document.get("license"), dict) else {}
    topics = [str(item) for item in document.get("topics") or [] if isinstance(item, str)]
    return {
        "full_name": _text(document.get("full_name"), 200),
        "description": _text(document.get("description")),
        "html_url": _text(document.get("html_url"), 300),
        "homepage": _text(document.get("homepage"), 300),
        "language": _text(document.get("language"), 50),
        "license": _text(license_info.get("spdx_id") or license_info.get("name"), 100),
        "stars": _number(document.get("stargazers_count")),
        "forks": _number(document.get("forks_count")),
        "open_issues": _number(document.get("open_issues_count")),
        "default_branch": _text(document.get("default_branch"), 100),
        "archived": bool(document.get("archived")),
        "fork": bool(document.get("fork")),
        "created_at": _text(document.get("created_at"), 40),
        "pushed_at": _text(document.get("pushed_at"), 40),
        "topics": [_text(item, 50) for item in topics[:_LIST_LIMIT]],
    }


def summarize_github_release(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict) or not document.get("tag_name"):
        return None
    return {
        "tag": _text(document.get("tag_name"), 100),
        "name": _text(document.get("name"), 200),
        "published_at": _text(document.get("published_at"), 40),
        "prerelease": bool(document.get("prerelease")),
    }


def render_github_repo(facts: dict[str, Any], release: dict[str, Any] | None) -> str:
    lines = [
        f"GitHub repository: {facts['full_name']}",
        f"Description: {facts['description']}",
        f"URL: {facts['html_url']}",
        f"License: {facts['license'] or 'unknown'}; language: {facts['language'] or 'unknown'}",
        (f"Stars: {facts['stars']}; forks: {facts['forks']}; open issues: {facts['open_issues']}"),
        f"Last push: {facts['pushed_at']}; created: {facts['created_at']}",
    ]
    if facts["archived"]:
        lines.append("Repository is ARCHIVED")
    if facts["fork"]:
        lines.append("Repository is a fork")
    if facts["topics"]:
        lines.append("Topics: " + ", ".join(facts["topics"]))
    if release is not None:
        kind = " (pre-release)" if release["prerelease"] else ""
        lines.append(f"Latest release: {release['tag']}{kind} published {release['published_at']}")
    return "\n".join(lines)


def summarize_github_search(document: Any, limit: int) -> list[dict[str, Any]]:
    """Repository hits from ``GET /search/repositories``."""
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise SourceFormatError("not a GitHub search document")
    hits: list[dict[str, Any]] = []
    for item in document["items"][:limit]:
        try:
            hits.append(summarize_github_repo(item))
        except ValueError:
            continue
    return hits


def render_github_search(query: str, hits: list[dict[str, Any]]) -> str:
    lines = [f"GitHub repository search: {query!r} ({len(hits)} results)"]
    for index, hit in enumerate(hits, 1):
        lines.append(
            f"{index}. {hit['full_name']} - stars {hit['stars']}, license "
            f"{hit['license'] or 'unknown'}, last push {hit['pushed_at']}: {hit['description']}"
        )
    return "\n".join(lines)


# -- Sphinx documentation -----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DocHit:
    title: str
    url: str
    kind: str
    score: float


def parse_objects_inv(data: bytes, base_url: str, *, max_bytes: int = MAX_INVENTORY_BYTES):
    """Entries of a Sphinx v2 ``objects.inv`` as ``(name, role, display, url)`` tuples."""
    lines = data.split(b"\n", 4)
    if len(lines) < 5 or not lines[0].startswith(b"# Sphinx inventory version 2"):
        raise ValueError("unsupported Sphinx inventory")
    if b"zlib" not in lines[3]:
        raise ValueError("Sphinx inventory is not zlib-compressed")
    inflater = zlib.decompressobj()
    # Bounded decompression: a tiny zlib bomb cannot expand past max_bytes.
    payload = inflater.decompress(lines[4], max_bytes)
    entries: list[tuple[str, str, str, str]] = []
    pattern = re.compile(r"^(.+?)\s+(\S+:\S+)\s+(-?\d+)\s+(\S*)\s+(.*)$")
    for raw in payload.decode("utf-8", errors="replace").splitlines():
        match = pattern.match(raw)
        if match is None:
            continue
        name, role, _priority, location, display = match.groups()
        if location.endswith("$"):
            location = location[:-1] + name
        display = name if display == "-" else display
        entries.append((name, role, display, urljoin(base_url, location)))
    return entries


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def search_inventory(
    entries: list[tuple[str, str, str, str]], query: str, limit: int
) -> list[DocHit]:
    terms = _tokens(query)
    if not terms:
        return []
    phrase = query.strip().lower()
    scored: list[DocHit] = []
    for name, role, display, url in entries:
        haystack = f"{name} {display}".lower()
        words = _tokens(haystack.replace(".", " "))
        matched = terms & words
        partial = sum(1 for term in terms - matched if term in haystack)
        if not matched and not partial:
            continue
        score = len(matched) * 2.0 + partial
        if phrase and phrase in haystack:
            score += 3.0
        if role in {"std:doc", "std:label"}:
            score += 0.5
        scored.append(DocHit(_text(display, 200), url, role, score / (len(terms) * 2.0 + 3.5)))
    scored.sort(key=lambda hit: (-hit.score, len(hit.title), hit.url))
    unique: dict[str, DocHit] = {}
    for hit in scored:
        unique.setdefault(hit.url, hit)
        if len(unique) >= limit:
            break
    return list(unique.values())


_SETINDEX = re.compile(r"Search\.setIndex\((.*)\)\s*;?\s*$", re.DOTALL)
_BARE_KEY = re.compile(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:(?=[\s\[{"\d-])')


def parse_searchindex(text: str) -> dict[str, Any]:
    """The index object of a Sphinx ``searchindex.js``.

    Modern Sphinx emits JSON; older versions emit JS literals with bare keys, which are quoted
    best-effort. The text is only ever parsed as data, never evaluated.
    """
    match = _SETINDEX.search(text.strip())
    if match is None:
        raise ValueError("not a Sphinx search index")
    body = match.group(1)
    try:
        index = json.loads(body)
    except json.JSONDecodeError:
        index = json.loads(_BARE_KEY.sub(r'\1"\2":', body))
    if not isinstance(index, dict) or not isinstance(index.get("docnames"), list):
        raise SourceFormatError("Sphinx search index has no docnames")
    return index


def search_searchindex(index: dict[str, Any], base_url: str, query: str, limit: int):
    docnames = [str(name) for name in index.get("docnames", [])]
    titles = [str(title) for title in index.get("titles", [])]
    terms = _tokens(query)
    scores: dict[int, float] = {}
    for bucket, weight in (("titleterms", 3.0), ("terms", 1.0)):
        mapping = index.get(bucket)
        if not isinstance(mapping, dict):
            continue
        for term in terms:
            documents = mapping.get(term)
            if isinstance(documents, int):
                documents = [documents]
            if not isinstance(documents, list):
                continue
            for doc in documents:
                if isinstance(doc, int) and 0 <= doc < len(docnames):
                    scores[doc] = scores.get(doc, 0.0) + weight
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    best = max((score for _, score in ranked), default=1.0)
    return [
        DocHit(
            _text(titles[doc] if doc < len(titles) else docnames[doc], 200),
            urljoin(base_url, f"{docnames[doc]}.html"),
            "page",
            round(score / best, 3),
        )
        for doc, score in ranked
    ]


def render_doc_hits(base_url: str, query: str, hits: list[DocHit], method: str) -> str:
    lines = [f"Documentation search on {base_url} for {query!r} via {method} ({len(hits)} hits)"]
    for index, hit in enumerate(hits, 1):
        lines.append(f"{index}. {hit.title} [{hit.kind}] {hit.url}")
    return "\n".join(lines)
