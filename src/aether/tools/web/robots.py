"""robots.txt parsing and an in-memory per-origin rules cache (stdlib ``urllib.robotparser``)."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

# Bounded so a hostile robots.txt cannot exhaust memory or parsing time.
MAX_ROBOTS_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """Crawl rules for one origin. ``reason`` explains how they were derived (for audit)."""

    parser: RobotFileParser | None
    reason: str
    allow_all: bool = False

    def allows(self, url: str, user_agent: str) -> bool:
        if self.allow_all:
            return True
        if self.parser is None:
            return False
        return self.parser.can_fetch(user_agent, url)

    def crawl_delay(self, user_agent: str) -> float | None:
        if self.parser is None:
            return None
        delay = self.parser.crawl_delay(user_agent)
        return float(delay) if delay is not None else None


def allow_all(reason: str) -> RobotsRules:
    return RobotsRules(None, reason, allow_all=True)


def disallow_all(reason: str) -> RobotsRules:
    return RobotsRules(None, reason)


def parse_robots(text: str, reason: str = "parsed") -> RobotsRules:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    return RobotsRules(parser, reason)


def rules_for_response(status_code: int, content_type: str, text: str) -> RobotsRules:
    """RFC 9309: 4xx means no restrictions; 5xx or an unreachable file means disallow all."""
    if 400 <= status_code < 500:
        return allow_all(f"robots.txt unavailable (HTTP {status_code})")
    if status_code >= 500:
        return disallow_all(f"robots.txt server error (HTTP {status_code})")
    if status_code != 200:
        return disallow_all(f"robots.txt unexpected status (HTTP {status_code})")
    if content_type and not content_type.startswith("text/plain"):
        # Soft-404 pages (HTML served for robots.txt) carry no rules.
        return allow_all(f"robots.txt served as {content_type}")
    return parse_robots(text)


class RobotsCache:
    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, RobotsRules]] = {}

    def get(self, origin: str) -> RobotsRules | None:
        entry = self._entries.get(origin)
        if entry is None:
            return None
        expires, rules = entry
        if expires <= self._clock():
            del self._entries[origin]
            return None
        return rules

    def put(self, origin: str, rules: RobotsRules) -> None:
        self._entries[origin] = (self._clock() + self._ttl, rules)
