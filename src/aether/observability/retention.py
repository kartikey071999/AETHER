"""Archive only rotated category logs, never active logs or the event spool."""

import gzip
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from aether.observability.logging import LOG_CATEGORIES

_ROTATED = re.compile(rf"^(?:{'|'.join(LOG_CATEGORIES)})\.log\.[1-9][0-9]*$")
_ARCHIVE = re.compile(rf"^(?:{'|'.join(LOG_CATEGORIES)})\.log\.[1-9][0-9]*(?:\..+)?\.gz$")


@dataclass(frozen=True)
class RetentionResult:
    archived: int
    deleted: int


def apply_retention(
    log_dir: str | Path,
    *,
    archive_after_days: int,
    delete_archives_after_days: int,
    now: datetime | None = None,
) -> RetentionResult:
    now = datetime.now(UTC) if now is None else now
    if now.utcoffset() is None:
        raise ValueError("retention time must include a timezone")
    if archive_after_days < 0 or delete_archives_after_days < 0:
        raise ValueError("retention periods cannot be negative")
    directory = Path(log_dir)
    if not directory.exists():
        return RetentionResult(archived=0, deleted=0)
    archive_dir = directory / "archive"
    if archive_dir.is_symlink():
        raise ValueError("archive directory must not be a symlink")
    archive_cutoff = (now - timedelta(days=archive_after_days)).timestamp()
    delete_cutoff = (now - timedelta(days=delete_archives_after_days)).timestamp()
    deleted = 0
    if archive_dir.exists():
        for path in archive_dir.iterdir():
            if (
                not path.is_symlink()
                and path.is_file()
                and _ARCHIVE.fullmatch(path.name)
                and path.stat().st_mtime <= delete_cutoff
            ):
                path.unlink()
                deleted += 1
    archived = 0
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file() or not _ROTATED.fullmatch(path.name):
            continue
        stat = path.stat()
        if stat.st_mtime > archive_cutoff:
            continue
        archive_dir.mkdir(exist_ok=True)
        destination = archive_dir / f"{path.name}.{stat.st_mtime_ns}.{uuid4().hex}.gz"
        temporary: Path | None = None
        try:
            with (
                path.open("rb") as source,
                tempfile.NamedTemporaryFile(dir=archive_dir, delete=False) as target,
            ):
                temporary = Path(target.name)
                with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
                    while chunk := source.read(1024 * 1024):
                        compressed.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            os.utime(temporary, (now.timestamp(), now.timestamp()))
            os.replace(temporary, destination)
            path.unlink()
            archived += 1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return RetentionResult(archived=archived, deleted=deleted)
