"""Read-only resource history API; the caller owns the database and server lifecycle."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.repositories._events import as_utc
from aether.storage.repositories.resources import ResourceMeasurementRepository


class ResourceMeasurementView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID | None
    cpu_percent: float
    available_ram_bytes: int
    free_disk_bytes: int
    observed_at: AwareDatetime

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at_utc(cls, value: datetime) -> datetime | None:
        return as_utc(value)


def create_app(sessions: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="AETHER")
    repository = ResourceMeasurementRepository(sessions)

    @app.get("/resources", response_model=list[ResourceMeasurementView])
    async def resources(
        since: Annotated[AwareDatetime | None, Query()] = None,
        until: Annotated[AwareDatetime | None, Query()] = None,
        task_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[ResourceMeasurementView]:
        if since is not None and until is not None and since >= until:
            raise HTTPException(status_code=422, detail="since must precede until")
        rows = await repository.range(
            since=since, until=until, task_id=task_id, limit=limit, offset=offset
        )
        return [ResourceMeasurementView.model_validate(row) for row in rows]

    return app
