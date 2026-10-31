"""Politeness controls: per-host request spacing and GitHub API rate-limit tracking."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping


class HostRateLimiter:
    """Space requests to each host by at least ``interval`` seconds (or a larger crawl delay)."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next: dict[str, float] = {}
        self._delays: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_crawl_delay(self, host: str, seconds: float, *, maximum: float = 30.0) -> None:
        self._delays[host] = min(max(seconds, 0.0), maximum)

    def interval_for(self, host: str) -> float:
        return max(self.interval, self._delays.get(host, 0.0))

    async def wait(self, host: str) -> float:
        """Block until ``host`` may be contacted again; return the seconds waited."""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = self._clock()
            ready = self._next.get(host, now)
            delay = max(ready - now, 0.0)
            if delay:
                await self._sleep(delay)
            self._next[host] = max(now, ready) + self.interval_for(host)
            return delay


MAX_BLOCK_SECONDS = 3600.0
SECONDARY_LIMIT_BACKOFF_SECONDS = 60.0


class GitHubRateLimit:
    """Refuse GitHub API calls once the quota is exhausted, until the advertised reset time.

    Reads ``X-RateLimit-Remaining``/``-Reset`` and ``Retry-After``; a 429 without a usable
    reset time (a secondary limit) backs off for a fixed minute.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._blocked_until: dict[str, float] = {}

    def blocked_for(self, bucket: str) -> float:
        until = self._blocked_until.get(bucket, 0.0)
        return max(until - self._clock(), 0.0)

    def observe(self, bucket: str, status_code: int, headers: Mapping[str, str]) -> None:
        lowered = {name.lower(): value for name, value in headers.items()}
        try:
            remaining = int(lowered.get("x-ratelimit-remaining", "1"))
            reset = float(lowered.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        now = self._clock()
        retry_after = lowered.get("retry-after")
        if retry_after is not None and status_code in {403, 429}:
            try:
                reset = max(reset, now + float(retry_after))
            except ValueError:
                pass
            remaining = 0
        elif status_code == 429:
            reset = max(reset, now + SECONDARY_LIMIT_BACKOFF_SECONDS)
            remaining = 0
        if remaining <= 0 and reset > now:
            # Never trust a far-future reset from the wire: cap the block at one hour.
            self._blocked_until[bucket] = min(reset, now + MAX_BLOCK_SECONDS)
