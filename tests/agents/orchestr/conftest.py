"""Shared fixtures for ORCHESTR tests: SQLite database, in-memory bus, fake host monitor."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from orchestr_helpers import FakeBus, FakeClock, FakeMonitor, Harness
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.agents.orchestr import OrchestrAgent
from aether.agents.orchestr.store import OrchestrStore
from aether.runtime.scheduler import SchedulerPolicy
from aether.storage.database import Database


@pytest.fixture(autouse=True)
def _mock_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AETHER_PROVIDER_MOCK", "1")
    for name in list(os.environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = schema_database(tmp_path / "orchestr.db")
    yield database
    await database.close()


@pytest.fixture
def harness(database: Database, tmp_path: Path) -> Harness:
    bus = FakeBus()
    clock = FakeClock()
    monitor = FakeMonitor()
    ctx = AgentContext(
        sessions=database.sessions,
        bus=bus,  # type: ignore[arg-type]
        clock=clock,
        services={
            "resource_monitor": monitor,
            "scheduler_policy": SchedulerPolicy(max_concurrent_tasks=4, max_concurrent_builds=1),
            "disk_path": tmp_path,
        },
    )
    return Harness(
        database=database,
        bus=bus,
        clock=clock,
        monitor=monitor,
        ctx=ctx,
        agent=OrchestrAgent(),
        store=OrchestrStore(database.sessions),
    )
