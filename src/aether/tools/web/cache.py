"""Content-addressed on-disk response cache for web research (under ``data_dir/web_cache``).

Entries are a JSON metadata file plus the raw body; the body's SHA-256 is verified on every
read, so a corrupted or tampered body is treated as a miss. Cached bytes stay untrusted.
"""

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CACHE_FORMAT = 1


@dataclass(frozen=True, slots=True)
class CachedResponse:
    url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    sha256: str
    stored_at: float
    truncated: bool


def cache_key(variant: str, url: str) -> str:
    return hashlib.sha256(f"{variant}\n{url}".encode()).hexdigest()


class WebCache:
    def __init__(
        self, root: Path, ttl_seconds: float, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.root = root
        self._ttl = ttl_seconds
        self._clock = clock

    def _paths(self, key: str) -> tuple[Path, Path]:
        folder = self.root / key[:2]
        return folder / f"{key}.json", folder / f"{key}.body"

    def get(self, variant: str, url: str) -> CachedResponse | None:
        if self._ttl <= 0:
            return None
        meta_path, body_path = self._paths(cache_key(variant, url))
        try:
            meta: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
            body = body_path.read_bytes()
            if (
                meta.get("format") != CACHE_FORMAT
                or meta.get("url") != url
                or meta.get("variant") != variant
                or hashlib.sha256(body).hexdigest() != meta.get("sha256")
            ):
                return None
            stored_at = float(meta["stored_at"])
            if stored_at + self._ttl <= self._clock():
                return None
            return CachedResponse(
                url=url,
                final_url=str(meta["final_url"]),
                status_code=int(meta["status_code"]),
                content_type=str(meta.get("content_type", "")),
                body=body,
                sha256=str(meta["sha256"]),
                stored_at=stored_at,
                truncated=bool(meta.get("truncated", False)),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(
        self,
        variant: str,
        url: str,
        *,
        final_url: str,
        status_code: int,
        content_type: str,
        body: bytes,
        truncated: bool,
    ) -> CachedResponse | None:
        if self._ttl <= 0:
            return None
        meta_path, body_path = self._paths(cache_key(variant, url))
        digest = hashlib.sha256(body).hexdigest()
        stored_at = self._clock()
        meta = {
            "format": CACHE_FORMAT,
            "variant": variant,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "sha256": digest,
            "stored_at": stored_at,
            "truncated": truncated,
        }
        try:
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(body_path, body)
            _atomic_write(meta_path, json.dumps(meta, sort_keys=True).encode("utf-8"))
        except OSError:
            return None
        return CachedResponse(
            url=url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            body=body,
            sha256=digest,
            stored_at=stored_at,
            truncated=truncated,
        )


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
