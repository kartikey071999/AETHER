"""Static dashboard assets, versioned by content hash so browsers can cache them safely."""

import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

STATIC = Path(__file__).parent / "static"
PREFIX = "/dashboard/static/"
TYPES = {
    "dashboard.css": "text/css; charset=utf-8",
    "theme.js": "text/javascript; charset=utf-8",
    "core.js": "text/javascript; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


@dataclass(frozen=True)
class Asset:
    body: bytes
    media_type: str
    version: str

    @property
    def etag(self) -> str:
        return f'"{self.version}"'


@cache
def asset(name: str) -> Asset | None:
    # Only the fixed allow-list is readable; the name never becomes a filesystem path otherwise.
    media_type = TYPES.get(name)
    if media_type is None:
        return None
    body = (STATIC / name).read_bytes()
    return Asset(body, media_type, hashlib.sha256(body).hexdigest()[:16])


def url(name: str) -> str:
    item = asset(name)
    if item is None:
        raise KeyError(name)
    return f"{PREFIX}{name}?v={item.version}"
