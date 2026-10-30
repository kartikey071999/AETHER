"""Redacted JSON-lines logging with bounded, category-specific files."""

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from aether.observability.events import AetherEvent, EventCategory
from aether.observability.redaction import redact, redact_string

LOG_CATEGORIES = (
    "runtime",
    "agent",
    "copilot",
    "git",
    "security",
    "test",
    "resource",
    "error",
    "decision",
)
_ROUTES = {
    EventCategory.OBJECTIVE: "runtime",
    EventCategory.TASK: "runtime",
    EventCategory.MESSAGE: "runtime",
    EventCategory.CAPABILITY: "agent",
}
_STANDARD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "exc_text",
}
_HANDLER_MARKER = "_aether_observability_handler"


class LoggingWriteError(RuntimeError):
    """A configured logging handler could not write its output."""


def _route(category: str) -> str:
    return _ROUTES.get(category, category)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_string(record.getMessage())
        record.args = ()
        for name in record.__dict__.keys() - _STANDARD_FIELDS:
            record.__dict__[name] = redact(record.__dict__[name])
        return True


class _CategoryFilter(logging.Filter):
    def __init__(self, category: str) -> None:
        super().__init__()
        self.category = category

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name.removeprefix("aether.").partition(".")[0]
        route = _route(name) if name in EventCategory else "runtime"
        if self.category == "error":
            return route == "error" or record.levelno >= logging.ERROR
        return route == self.category


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
        }
        conflicts: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS:
                if key in document or key == "extra":
                    conflicts[key] = value
                else:
                    document[key] = value
        if conflicts:
            document["extra"] = conflicts
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            document["stack"] = record.stack_info
        return json.dumps(
            redact(document),
            ensure_ascii=True,
            default=lambda value: redact_string(str(value)),
        )


class _ReliableRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise LoggingWriteError("Failed to write structured category log") from sys.exception()


class _ReliableConsoleHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        raise LoggingWriteError("Failed to write structured console log") from sys.exception()


def configure_logging(
    log_dir: str | Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    level: int | str = logging.INFO,
    console: bool = False,
) -> None:
    """Replace only handlers owned here; never accumulate duplicate handlers."""
    if max_bytes <= 0 or backup_count < 1:
        raise ValueError("max_bytes and backup_count must be positive")
    if isinstance(level, str):
        try:
            level = logging.getLevelNamesMapping()[level.upper()]
        except KeyError as error:
            raise ValueError("unknown logging level") from error
    if not isinstance(level, int) or isinstance(level, bool) or level < 0:
        raise ValueError("logging level must be a nonnegative integer or level name")
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aether")
    for handler in logger.handlers[:]:
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(min(level, logging.ERROR))
    logger.propagate = False
    formatter = JsonLinesFormatter()
    for category in LOG_CATEGORIES:
        handler = _ReliableRotatingFileHandler(
            directory / f"{category}.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            delay=True,
            encoding="utf-8",
        )
        handler.setLevel(min(level, logging.ERROR) if category == "error" else level)
        handler.setFormatter(formatter)
        handler.addFilter(_CategoryFilter(category))
        handler.addFilter(RedactionFilter())
        setattr(handler, _HANDLER_MARKER, True)
        logger.addHandler(handler)
    if console:
        handler = _ReliableConsoleHandler()
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.addFilter(RedactionFilter())
        setattr(handler, _HANDLER_MARKER, True)
        logger.addHandler(handler)


def get_logger(category: EventCategory | str) -> logging.Logger:
    category = EventCategory(category)
    return logging.getLogger(f"aether.{category.value}")


def log_event(event: AetherEvent) -> None:
    level = logging.ERROR if event.category == EventCategory.ERROR else logging.INFO
    get_logger(event.category).log(
        level, event.event_type, extra={"event": event.model_dump(mode="json")}
    )
