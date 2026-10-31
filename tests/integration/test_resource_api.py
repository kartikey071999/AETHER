import asyncio
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from aether.api import create_app
from aether.api.schemas import Page
from aether.config import AetherSettings
from aether.models.settings import ProviderSettings
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.resource_monitor import HostResources
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.storage.database import Database
from aether.storage.models import Base, ResourceMeasurementRecord
from aether.storage.repositories import (
    ObjectiveRepository,
    ResourceMeasurementRepository,
    TaskRepository,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def app(database):
    return create_app(
        AetherSettings(_env_file=None, database_url=str(database.engine.url), api_token=None),
        database=database,
        provider_settings=ProviderSettings(_env_file=None),
    )


class FakeMonitor:
    def snapshot(self, disk_path):
        return HostResources(cpu_percent=10, available_ram_bytes=1234, free_disk_bytes=5678)


async def test_runtime_persists_periodic_samples_and_api_returns_rows(database, app):
    engine = Engine(
        database.sessions,
        [],
        Scheduler(SchedulerPolicy()),
        FakeMonitor(),
        EngineLimits(resource_sample_seconds=1, shutdown_timeout_seconds=1),
    )
    await engine.start()
    try:
        # No tasks or scheduler resource reads: the independent sampler must still run.
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            async with database.sessions() as session:
                count = len(list(await session.scalars(select(ResourceMeasurementRecord))))
            if count >= 2:
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/resources")
        assert response.status_code == 200
        page = Page.model_validate(response.json())
        rows = page.items
        assert page.total >= len(rows) and page.limit == 100 and page.offset == 0
        assert len(rows) >= 2
        assert set(rows[0]) == {
            "id",
            "task_id",
            "cpu_percent",
            "available_ram_bytes",
            "free_disk_bytes",
            "observed_at",
        }
        assert rows[0]["cpu_percent"] == 10
        times = [datetime.fromisoformat(row["observed_at"]) for row in rows]
        assert all((earlier - later).total_seconds() >= 1 for earlier, later in pairwise(times))
        assert all(time.utcoffset() == timedelta(0) for time in times)
    finally:
        await engine.stop()


async def test_api_filters_and_rejects_invalid_queries(database, app):
    repository = ResourceMeasurementRepository(database.sessions)
    for seconds in (0, 30, 60):
        await repository.record(
            cpu_percent=seconds,
            available_ram_bytes=1,
            free_disk_bytes=2,
            observed_at=NOW + timedelta(seconds=seconds),
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/resources",
            params={
                "since": NOW.isoformat(),
                "until": (NOW + timedelta(seconds=60)).isoformat(),
                "limit": 1,
            },
        )
        assert response.status_code == 200 and response.json()["items"][0]["cpu_percent"] == 30
        assert response.json()["total"] == 2 and response.json()["limit"] == 1
        response = await client.get("/resources", params={"limit": 1, "offset": 1})
        assert response.json()["items"][0]["cpu_percent"] == 30
        assert response.json()["total"] == 3 and response.json()["offset"] == 1
        assert (await client.get("/resources", params={"limit": 1000})).status_code == 200
        response = await client.get("/resources", params={"offset": 3})
        assert response.json()["items"] == [] and response.json()["total"] == 3
        for params in (
            {"limit": 1001},
            {"limit": 0},
            {"offset": -1},
            {"task_id": "invalid"},
            {"since": NOW.isoformat(), "until": NOW.isoformat()},
            {"since": (NOW + timedelta(seconds=1)).isoformat(), "until": NOW.isoformat()},
            {"since": "2030-01-01T00:00:00"},
            {"until": "2030-01-01T00:00:00"},
        ):
            assert (await client.get("/resources", params=params)).status_code == 422


async def test_empty_resources_returns_empty_items(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/resources")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


async def test_resource_filters_by_task_and_normalizes_timezones(database, app):
    objective_id = await ObjectiveRepository(database.sessions).create(title="Resource history")
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id, title="Measured task"
    )
    repository = ResourceMeasurementRepository(database.sessions)
    for seconds, task in ((0, task_id), (30, None), (60, task_id)):
        await repository.record(
            cpu_percent=seconds,
            available_ram_bytes=1,
            free_disk_bytes=2,
            observed_at=NOW + timedelta(seconds=seconds),
            task_id=task,
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/resources", params={"task_id": str(task_id)})
        assert response.status_code == 200 and response.json()["total"] == 2
        rows = response.json()["items"]
        assert [row["cpu_percent"] for row in rows] == [60, 0]
        assert all(row["task_id"] == str(task_id) for row in rows)
        response = await client.get(
            "/resources",
            params={
                "task_id": str(task_id),
                "since": "2030-01-01T05:30:00+05:30",
                "until": "2030-01-01T05:31:00+05:30",
            },
        )
        assert response.status_code == 200 and response.json()["total"] == 1
        assert response.json()["items"][0]["observed_at"] == NOW.isoformat()
        for params in ({"task_id": str(uuid4())}, {"since": (NOW + timedelta(days=1)).isoformat()}):
            response = await client.get("/resources", params=params)
            assert response.json()["items"] == [] and response.json()["total"] == 0
