import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlite_schema import schema_database

from aether.observability.events import AetherEvent, build_event
from aether.storage.database import Database


@pytest.fixture
def event() -> AetherEvent:
    return build_event(
        "error.raised",
        source="unit-test",
        actor="system",
        aggregate_type="runtime",
        aggregate_id=uuid4(),
        payload={
            "error_type": "ExampleError",
            "message": "safe diagnostic",
            "details": {"api_key": "do-not-store", "nested": {"password": "do-not-store"}},
        },
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = schema_database(tmp_path / "events.db")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    logger = logging.getLogger("aether")
    handlers = logger.handlers[:]
    level, propagate = logger.level, logger.propagate
    logger.handlers = []
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
