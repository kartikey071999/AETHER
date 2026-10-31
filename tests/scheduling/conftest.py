from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from reliability_support import FakeClock, sqlite_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.database import Database
from aether.storage.repositories import ObjectiveRepository


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    async with sqlite_database(tmp_path / "scheduling.db") as database:
        yield database


@pytest_asyncio.fixture
async def peer_database(tmp_path, database: Database) -> AsyncIterator[Database]:
    """A second, independent connection pool on the same file: another runtime process."""
    peer = Database(f"sqlite+aiosqlite:///{(tmp_path / 'scheduling.db').as_posix()}")
    try:
        yield peer
    finally:
        await peer.close()


@pytest.fixture
def sessions(database: Database) -> async_sessionmaker[AsyncSession]:
    return database.sessions


@pytest_asyncio.fixture
async def objective_id(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    return await ObjectiveRepository(sessions).create(title="Scheduling objective")
