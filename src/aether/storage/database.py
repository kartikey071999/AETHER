"""Async SQLAlchemy engine and session lifecycle."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aether.config import AetherSettings


class Database:
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
        )
        self.sessions = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @classmethod
    def from_settings(cls, settings: AetherSettings) -> "Database":
        return cls(settings.database_url.get_secret_value())

    async def close(self) -> None:
        await self.engine.dispose()
