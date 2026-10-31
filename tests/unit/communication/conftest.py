from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlite_schema import schema_database

from aether.storage.database import Database


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    db = schema_database(tmp_path / "bus.db")
    yield db
    await db.close()


@pytest.fixture
def sessions(database: Database) -> async_sessionmaker[AsyncSession]:
    return database.sessions
