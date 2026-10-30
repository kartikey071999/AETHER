from collections.abc import AsyncIterator, Awaitable, Callable
from uuid import UUID

import pytest
from sqlalchemy import select

from aether.storage.database import Database
from aether.storage.models import Base, EventRecord
from aether.storage.repositories import ObjectiveRepository

EventLoader = Callable[[UUID], Awaitable[list[EventRecord]]]


@pytest.fixture
async def database(tmp_path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'storage.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


@pytest.fixture
async def objective_id(database: Database) -> UUID:
    return await ObjectiveRepository(database.sessions).create(title="Storage objective")


@pytest.fixture
def events_for(database: Database) -> EventLoader:
    async def load(aggregate_id: UUID) -> list[EventRecord]:
        statement = select(EventRecord).where(EventRecord.aggregate_id == aggregate_id)
        async with database.sessions() as session:
            events = list(await session.scalars(statement))
        return sorted(events, key=lambda event: event.created_at)

    return load
