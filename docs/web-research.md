# Web research tools

SCOUT and ROGUE can read the public web through seven read-only tools in
`aether.tools.web`. Each request is a `web_fetch` operation that
`PermissionService` authorizes and the pinned `SafeHttp` exchange executes. Every
result is `UntrustedContent`. This document covers the library choice (T-052),
the security model, politeness, configuration and known limitations.

## Library comparison and recommendation

Snapshot of the PyPI JSON API, retrieved 2026-10-31. The SHA-256 of each
response is recorded as evidence in the seeded SCOUT finding
`tests/fixtures/research/web_research_tools_finding.json`.

| Option | Version | Licence | Runtime deps | Size / build | Windows | Maintenance | Security surface |
|---|---|---|---|---|---|---|---|
| **stdlib `html.parser`** | Python 3.12 | PSF | none | part of CPython | yes | CPython release cycle | pure Python tokenizer, no JS |
| selectolax | 1.0.2 | MIT | none | C extension (Lexbor), wheels up to ~1.6 MB | win_amd64 wheels | released 2026-10-22 | native parser code |
| beautifulsoup4 | 4.15.0 | MIT | soupsieve, typing-extensions | pure, ~110 KB | yes | released 2026-06-07 | pure Python; slow with `html.parser`, needs lxml for speed |
| trafilatura | 2.3.1 | Apache-2.0 | lxml, justext, htmldate, courlan, urllib3, charset_normalizer, certifi | pure wrapper over lxml | yes (via lxml wheels) | released 2026-10-06 | brings its own HTTP stack (urllib3), which must not bypass SafeHttp |
| readability-lxml | 0.9 | Apache-2.0 | lxml[html-clean], chardet, cssselect | pure wrapper over lxml | yes | released 2026-08-27 | lxml native code |
| markdownify | 1.2.3 | MIT | beautifulsoup4, six | pure, ~16 KB | yes | released 2026-06-30 | as BeautifulSoup |
| lxml | 6.1.3 | BSD-3-Clause | none (bundles libxml2/libxslt) | wheels up to ~8.9 MB | win_amd64 wheels | released 2026-09-03 | large native C codebase |
| resiliparse | 1.1.0 | Apache-2.0 | fastwarc | wheels up to ~5.7 MB | win_amd64 wheels | released 2026-10-26 | native code, crawl-scale focus |
| justext | 3.0.2 | BSD-2-Clause | lxml[html_clean] | pure over lxml | yes | released 2025-02-25 | lxml native code |
| html5lib | 1.1 | MIT | six, webencodings | pure | yes | last release 2020-06-22 | unmaintained |
| html2text | 2025.4.15 | **GPL-3.0-or-later** | none | pure | yes | released 2025-04-15 | licence incompatible with a permissive default |
| Playwright | 1.64.1 | Apache-2.0 | greenlet, pyee | ~48 MB wheel **plus** browser downloads | yes | released 2026-10-22 | executes untrusted JavaScript in a full browser that makes its own network requests, outside SafeHttp pinning and per-request policy |

**Recommendation: stdlib `html.parser` (adopted; no new dependency).** AETHER
mostly reads documentation and API metadata, not ad-heavy news pages. A small
extractor of our own (`aether.tools.web.extract`) does the job:

- It drops `script`, `style`, `template`, `iframe`, `svg`, navigation, footers
  and hidden elements (`hidden`, `aria-hidden`, `display:none`).
- It prefers `<main>` or `<article>` content and keeps headings, lists and
  `<pre>` blocks.
- It resolves links against `<base>` and honours `rel=nofollow` and
  `<meta name=robots content=nofollow>`.

It is pure Python and auditable, and it runs anywhere AETHER runs.

- **Optional upgrade:** `selectolax==1.0.2`, if extraction speed or CSS
  selectors become necessary. It is MIT-licensed, has no dependencies and ships
  Windows wheels. Adding it needs coordinator review because it is native code.
- **Rejected:**
  - Playwright and other headless browsers: they run JavaScript and make
    network requests that the per-request security path cannot gate.
  - html2text: GPL licence.
  - html5lib: unmaintained.
  - trafilatura and readability-lxml: they pull in lxml plus a separate HTTP
    stack and are tuned for articles rather than reference docs.

## Tools

| Tool | Purpose |
|---|---|
| `web.fetch` | Size-, time- and content-type-limited GET. Returns the decoded body. |
| `web.extract` | HTML to readable text, plus title, headings, links and metadata (description, canonical, Open Graph, generator, language). |
| `web.links` | Breadth-first, same-host link discovery. Depth is 0-3, capped at `max_crawl_pages`. Robots.txt is checked for every page; `nofollow` pages are not expanded. |
| `pypi.package` | Key facts from `https://pypi.org/pypi/<name>[/<version>]/json`: version, licence, Python requirement, dependencies, latest upload, project URLs. |
| `github.repo` | Repository metadata, licence and (optionally) the latest release from the unauthenticated GitHub REST API. |
| `github.search` | Repository search on GitHub, used by ROGUE to find alternatives. |
| `docs.search` | Search a Sphinx site on a configured docs host. It tries `objects.inv` first (bounded zlib inflate), then falls back to `searchindex.js`, which is parsed as data and never evaluated. |

Every result is a `WebResult`:

- `body`: the citable `UntrustedContent` text.
- `raw_sha256`: the hash of the exact bytes received.
- Structured `facts`, `links`, `pages` and `hits`.
- `from_cache`.
- `notes`, recording what was skipped and why.

## Security model

- **Separate operation kind.** `web_fetch` is granted only to SCOUT and ROGUE
  (via `research.create`). It is checked only against the dedicated web
  allowlist (`policy.web_allowed_hosts`), never against
  `AETHER_SECURITY_ALLOWED_HOSTS`. The reverse also holds: web hosts do not
  widen `http_request`.
- **One operation per request.** The primary URL uses the tool call's grant.
  Every other request needs its own authorized and audited `web_fetch`
  operation, with a recorded `purpose`. That covers robots.txt, crawl pages,
  release lookups and searchindex fallbacks.
- **Request rules.** SafeHttp pins DNS: each hop resolves once, every resolved
  address must be public, and the connection goes to the vetted IP with SNI and
  Host for the name.
  - Private, loopback, link-local and metadata addresses are always refused
    for web research.
  - Redirects are re-checked against the allowlist on every hop.
  - Only GET is used, and no credentials are sent.
- **Content and size limits.**
  - Content types are checked before the body is read.
  - Bodies are capped at `min(max_page_bytes, AETHER_SECURITY_MAX_RESPONSE_BYTES)`.
  - Extracted text, link and result counts are bounded.
- **Untrusted data.** Page text is `UntrustedContent`. Instruction-like content
  is flagged and quarantined: SCOUT does not send it to models, and it can
  never change permissions or decisions. GitHub `owner/repo` names taken from
  search results become follow-up arguments only after strict validation.
- **Audit trail.** Every request creates an execution run with the resource
  usage (tool, purpose, bytes, sha256, cache hit or miss) and `execution.*`
  audit events. Blocks are `security.operation_blocked` events.

## Politeness

- **robots.txt** (RFC 9309) is fetched per origin through its own authorized
  operation and cached for `robots_ttl_seconds`.
  - 4xx means no restrictions.
  - 5xx, unreachable, blocked by policy, or redirected off the allowlist means
    disallow all.
  - `Crawl-delay` raises the per-host interval, capped at 30 s.
  - Rules are matched against the AETHER `User-Agent`.
- **Rate limiting:**
  - At least `per_host_interval_seconds` between requests to a host.
  - A global concurrency cap (`max_concurrency`).
  - GitHub `X-RateLimit-Remaining`, `X-RateLimit-Reset` and `Retry-After` are
    tracked per bucket (core and search). An exhausted bucket refuses calls
    locally until reset; the wait is capped at 1 h.
- **User-Agent** identifies AETHER: `AETHER-research/<version> (+local research agent; read-only; respects robots.txt)`.
- **Disk cache** lives in `data_dir/web_cache/`. Only 200 responses are
  stored. Entries expire after `cache_ttl_seconds` (0 disables the cache) and
  are written atomically. A body whose SHA-256 does not match its metadata is
  ignored and fetched again. Cache hits are still authorized and audited.

## Agent integration

Research scopes accept the web source kinds alongside `file` and `http`:

```json
{"sources":[
  {"kind":"docs","location":"https://docs.python.org/3/","query":"asyncio timeout"},
  {"kind":"pypi","location":"httpx"},
  {"kind":"github","location":"encode/httpx"},
  {"kind":"github_search","location":"python http client","perspective":"unconventional"},
  {"kind":"web","location":"https://docs.python.org/3/library/asyncio-task.html"}
]}
```

- **Opt-in derived research.** Set `AETHER_RESEARCH__WEB_RESEARCH=true`. A
  request with no scoped or default sources then derives two searches from
  keywords in the question:
  - a `docs` search on `AETHER_RESEARCH__DOCS_BASE_URL`, which SCOUT reads
    first;
  - a `github_search`, which ROGUE reads first because it reverses the order.
- **ROGUE follow-ups.** After a clean GitHub search, ROGUE follows up on up to
  `AETHER_RESEARCH__WEB_FOLLOW_UPS` repositories with `github.repo`.
- **Budget and citations.** Every source, including follow-ups, consumes the
  request's `max_sources` budget. Findings cite each source URL with the
  content hash of the evidence text.
- **Unavailable sources.** A web source that is blocked by robots or policy,
  failing, or returns non-2xx is logged as `research.source_skipped` and does
  not fail the task.

## Configuration

| Setting | Default |
|---|---|
| `AETHER_WEB_ALLOWED_HOSTS` | `["pypi.org","api.github.com","github.com","docs.python.org","readthedocs.io","*.readthedocs.io"]` |
| `AETHER_WEB_DOCS_HOSTS` | `["docs.python.org","*.readthedocs.io"]` (must also be allowed) |
| `AETHER_WEB_USER_AGENT` | AETHER research agent string |
| `AETHER_WEB_TIMEOUT_SECONDS` | 20 |
| `AETHER_WEB_MAX_PAGE_BYTES` | 2 MiB |
| `AETHER_WEB_ALLOWED_CONTENT_TYPES` | HTML, XHTML, plain text, Markdown, JSON, XML |
| `AETHER_WEB_CACHE_TTL_SECONDS` | 21600 (6 h) |
| `AETHER_WEB_ROBOTS_TTL_SECONDS` | 86400 |
| `AETHER_WEB_PER_HOST_INTERVAL_SECONDS` | 1.0 |
| `AETHER_WEB_MAX_CONCURRENCY` | 4 |
| `AETHER_WEB_MAX_CRAWL_PAGES` / `_MAX_CRAWL_DEPTH` | 10 / 2 |
| `AETHER_WEB_MAX_EXTRACT_CHARS` / `_MAX_LINKS` / `_MAX_SEARCH_RESULTS` | 20000 / 200 / 10 |
| `AETHER_RESEARCH__WEB_RESEARCH` | `false` |
| `AETHER_RESEARCH__DOCS_BASE_URL` | `https://docs.python.org/3/` |
| `AETHER_RESEARCH__WEB_FOLLOW_UPS` | 1 |

The web allowlist is frozen into the immutable security policy at startup.
`aether security show` lists it as `web_allowed_hosts`, and the `runtime.security_initialized` event reports its size as `web_hosts`.

## Assumptions and limitations

- **robots.txt and JSON APIs.** robots.txt applies to page tools (`web.*`,
  `docs.search`), not to the documented JSON APIs (`pypi.package`, `github.*`).
  pypi.org's robots.txt disallows `/pypi/*/json`, but PyPI documents that
  endpoint for programmatic clients. GitHub's API is governed by its rate
  limits.
- **Approval.** `web_fetch` is not approval-required by default. It is
  read-only and limited to the web allowlist. Operators can add `web_fetch` to
  `AETHER_SECURITY_APPROVAL_REQUIRED_KINDS` to require WARDEN review.
- **GitHub.** Calls are unauthenticated: 60 core and 10 search requests per
  hour per IP. Token support is a follow-up.
- **readthedocs.io.** The apex `readthedocs.io/robots.txt` redirects off the
  allowlist, so the apex is treated as disallowed. Project subdomains
  (`*.readthedocs.io`) work normally.
- **No JavaScript rendering.** Client-side-only pages yield little text.
- **Large search indexes.** docs.python.org's `searchindex.js` (~4 MB) exceeds
  the page cap, so Python docs searches rely on `objects.inv`, which matches
  API object names and labels rather than doing full-text search.
