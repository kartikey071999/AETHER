"""GitHub API credentials and the operator-facing rate-limit check (``aether web status``)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from aether.tools.web.settings import WebSettings

GITHUB_API_HOST = "api.github.com"
GITHUB_RATE_LIMIT_URL = f"https://{GITHUB_API_HOST}/rate_limit"


def is_github_api_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() == GITHUB_API_HOST


def github_auth_headers(url: str, token: SecretStr | None) -> dict[str, str]:
    """``Authorization`` for requests to exactly ``https://api.github.com``; nothing elsewhere."""
    if token is None or not is_github_api_url(url):
        return {}
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


@dataclass(frozen=True, slots=True)
class RateBucket:
    limit: int
    remaining: int
    reset: datetime | None


@dataclass(frozen=True, slots=True)
class GitHubStatus:
    token_configured: bool
    reachable: bool
    status_code: int | None = None
    buckets: dict[str, RateBucket] | None = None
    error: str | None = None


def _bucket(raw: Any) -> RateBucket | None:
    if not isinstance(raw, dict):
        return None
    try:
        reset = raw.get("reset")
        return RateBucket(
            int(raw["limit"]),
            int(raw["remaining"]),
            datetime.fromtimestamp(int(reset), UTC) if reset is not None else None,
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


async def fetch_github_status(
    settings: WebSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> GitHubStatus:
    """Ask ``GET /rate_limit`` (free of quota) for the current limits; never reveals the token.

    This is an operator diagnostic, not an agent tool: one fixed https URL, no redirects,
    no proxy settings from the environment.
    """
    configured = settings.github_token is not None
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **github_auth_headers(GITHUB_RATE_LIMIT_URL, settings.github_token),
    }
    try:
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(settings.timeout_seconds),
            trust_env=False,
        ) as client:
            response = await client.get(GITHUB_RATE_LIMIT_URL, headers=headers)
    except httpx.HTTPError as error:
        return GitHubStatus(configured, False, error=type(error).__name__)
    if response.status_code != 200:
        return GitHubStatus(configured, True, response.status_code)
    try:
        resources = response.json().get("resources", {})
    except (ValueError, AttributeError):
        return GitHubStatus(configured, True, response.status_code, error="unreadable response")
    buckets = {
        name: bucket
        for name in ("core", "search")
        if (bucket := _bucket(resources.get(name) if isinstance(resources, dict) else None))
    }
    return GitHubStatus(configured, True, response.status_code, buckets)
