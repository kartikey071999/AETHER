from sqlalchemy import select

from aether.storage.database import Database
from aether.storage.models import EventRecord, ProviderUsageRecord


async def records(database: Database) -> tuple[list[ProviderUsageRecord], list[EventRecord]]:
    async with database.sessions() as session:
        rows = list(await session.scalars(select(ProviderUsageRecord)))
        events = list(
            await session.scalars(
                select(EventRecord).where(EventRecord.event_type == "provider.request_completed")
            )
        )
    return rows, events
