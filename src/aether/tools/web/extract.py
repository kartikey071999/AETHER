"""Dependency-free readable-text extraction from untrusted HTML (stdlib ``html.parser``).

Scripts, styles, embedded documents and hidden elements are dropped; navigation boilerplate is
excluded from the readable text but its links are kept for discovery. When the page marks its
main content (``<main>``/``<article>``), only that content is returned as text. Everything
extracted remains untrusted data: nothing here interprets the page as instructions.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

# Content that is never text: code, styling, embedded documents and graphics.
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas"}
)
# Page furniture excluded from readable text (links inside still count for discovery).
_BOILERPLATE_TAGS = frozenset({"nav", "footer", "aside", "form", "button", "select", "dialog"})
_MAIN_TAGS = frozenset({"main", "article"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tr",
        "ul",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_META_KEYS = frozenset(
    {
        "author",
        "description",
        "generator",
        "keywords",
        "robots",
        "og:description",
        "og:site_name",
        "og:title",
        "og:type",
        "og:url",
    }
)
_HIDDEN_STYLE = re.compile(r"(display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE)
_SPACES = re.compile(r"[ \t\r\n\f\v\u00a0\u200b]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_META_VALUE_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    text: str = ""


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Readable view of one HTML document. All fields are untrusted page data."""

    title: str
    text: str
    headings: tuple[str, ...] = ()
    links: tuple[Link, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.metadata.get("robots", "").lower()

    def render(self) -> str:
        """Plain text for evidence: title, description and readable body."""
        parts = [f"Title: {self.title}"] if self.title else []
        description = self.metadata.get("description") or self.metadata.get("og:description")
        if description:
            parts.append(f"Description: {description}")
        parts.append(self.text)
        return "\n\n".join(part for part in parts if part)


def _is_hidden(attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    return bool(_HIDDEN_STYLE.search(attrs.get("style") or ""))


def _clean(value: str) -> str:
    return _SPACES.sub(" ", value).strip()


def normalize_url(url: str) -> str | None:
    """Absolute http(s) URL without fragment, or ``None`` for anything else."""
    try:
        url, _ = urldefrag(url.strip())
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return url


class _ReadableParser(HTMLParser):
    def __init__(self, base_url: str, max_links: int) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.max_links = max_links
        self.title_parts: list[str] = []
        self.metadata: dict[str, str] = {}
        self.headings: list[str] = []
        self.links: dict[str, str] = {}
        self.all_text: list[str] = []
        self.main_text: list[str] = []
        self._skip: list[tuple[str, int]] = []
        self._boilerplate = 0
        self._main = 0
        self._pre = 0
        self._in_title = False
        self._title_done = False
        self._heading: list[str] | None = None
        self._link: tuple[str, list[str]] | None = None

    # -- structure -------------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value for name, value in attrs}
        if self._skip:
            name, depth = self._skip[-1]
            if tag == name and tag not in _VOID_TAGS:
                self._skip[-1] = (name, depth + 1)
            return
        if tag in _SKIP_TAGS or (tag not in _VOID_TAGS and _is_hidden(values)):
            self._skip.append((tag, 1))
            return
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "meta":
            self._meta(values)
        elif tag == "link":
            rel = (values.get("rel") or "").lower().split()
            if "canonical" in rel and (href := values.get("href")):
                canonical = normalize_url(urljoin(self.base_url, href))
                if canonical:
                    self.metadata["canonical"] = canonical
        elif tag == "base" and (href := values.get("href")):
            base = normalize_url(urljoin(self.base_url, href))
            if base:
                self.base_url = base
        elif tag == "html" and (lang := values.get("lang")):
            self.metadata["lang"] = _clean(lang)[:32]
        elif tag == "a":
            self._close_link()
            href = values.get("href")
            url = normalize_url(urljoin(self.base_url, href)) if href else None
            if url is not None:
                self._link = (url, [])
        if tag in _BOILERPLATE_TAGS:
            self._boilerplate += 1
        if tag in _MAIN_TAGS:
            self._main += 1
        if tag == "pre":
            self._pre += 1
        if tag in _BLOCK_TAGS:
            self._newline()
        if tag in _HEADING_LEVELS:
            self._heading = []
            if _HEADING_LEVELS[tag] <= 3:
                self._emit("#" * _HEADING_LEVELS[tag] + " ", raw=True)
        elif tag == "li":
            self._emit("- ", raw=True)
        elif tag in {"td", "th"}:
            self._emit(" ", raw=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            name, depth = self._skip[-1]
            if tag == name:
                if depth > 1:
                    self._skip[-1] = (name, depth - 1)
                else:
                    self._skip.pop()
            return
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        elif tag == "a":
            self._close_link()
        if tag in _HEADING_LEVELS and self._heading is not None:
            heading = _clean("".join(self._heading))
            if heading:
                self.headings.append(heading)
            self._heading = None
        if tag in _BOILERPLATE_TAGS and self._boilerplate:
            self._boilerplate -= 1
        if tag in _MAIN_TAGS and self._main:
            self._main -= 1
        if tag == "pre" and self._pre:
            self._pre -= 1
        if tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._link is not None:
            self._link[1].append(data)
        if self._heading is not None:
            self._heading.append(data)
        if self._boilerplate:
            return
        self._emit(data, raw=bool(self._pre))

    # -- helpers ---------------------------------------------------------------------------
    def _meta(self, values: dict[str, str | None]) -> None:
        key = (values.get("name") or values.get("property") or "").strip().lower()
        content = values.get("content")
        if key in _META_KEYS and content and key not in self.metadata:
            self.metadata[key] = _clean(content)[:_META_VALUE_LIMIT]

    def _close_link(self) -> None:
        if self._link is None:
            return
        url, parts = self._link
        self._link = None
        if url not in self.links and len(self.links) < self.max_links:
            self.links[url] = _clean("".join(parts))[:200]

    def _targets(self) -> list[list[str]]:
        return [self.all_text, self.main_text] if self._main else [self.all_text]

    def _newline(self) -> None:
        for target in self._targets():
            if target and not target[-1].endswith("\n"):
                target.append("\n")

    def _emit(self, data: str, *, raw: bool = False) -> None:
        text = data if raw else _SPACES.sub(" ", data)
        if not text:
            return
        for target in self._targets():
            piece = text
            if not raw and (not target or target[-1].endswith(("\n", " "))):
                piece = piece.lstrip(" ")
            if piece:
                target.append(piece)


def _finish_text(pieces: list[str]) -> str:
    lines = [line.rstrip() for line in "".join(pieces).split("\n")]
    text = "\n".join(lines).strip("\n")
    return _BLANK_LINES.sub("\n\n", text)


def extract_html(
    html: str, base_url: str, *, max_chars: int = 20_000, max_links: int = 200
) -> ExtractedPage:
    """Readable text, title, headings, links and metadata of ``html`` fetched from ``base_url``."""
    parser = _ReadableParser(base_url, max_links)
    parser.feed(html)
    parser.close()
    parser._close_link()
    main = _finish_text(parser.main_text)
    text = main if main.strip() else _finish_text(parser.all_text)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    title = _clean("".join(parser.title_parts)) or parser.metadata.get("og:title", "")
    return ExtractedPage(
        title=title[:_META_VALUE_LIMIT],
        text=text,
        headings=tuple(parser.headings[:100]),
        links=tuple(Link(url, label) for url, label in parser.links.items()),
        metadata=dict(parser.metadata),
        truncated=truncated,
    )
