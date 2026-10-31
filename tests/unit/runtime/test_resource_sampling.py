import asyncio
from datetime import timedelta

import pytest
from runtime_helpers import FakeMonitor, stub_for

from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.runtime.engine import Engine, EngineLimits
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.storage.repositories import ExecutionRunRepository, ResourceMeasurementRepository


def build_engine(sessions, clock, **limits):
    return Engine(
        sessions,
        [],
        Scheduler(SchedulerPolicy()),
        FakeMonitor(),
        EngineLimits(**limits),
        clock=clock,
    )


async def test_sampling_is_bounded_even_for_concurrent_calls(sessions, clock):
    engine = build_engine(sessions, clock)
    assert sum(await asyncio.gather(*(engine.sample_resources_once() for _ in range(5)))) == 1
    clock.advance(29)
    assert await engine.sample_resources_once() is False
    clock.advance(1)
    assert await engine.sample_resources_once() is True
    assert len(await ResourceMeasurementRepository(sessions).range()) == 2


async def test_startup_and_daily_retention(sessions, clock, monkeypatch):
    repository = ResourceMeasurementRepository(sessions)
    sample = {"cpu_percent": 1, "available_ram_bytes": 2, "free_disk_bytes": 3}
    await repository.record(**sample, observed_at=clock() - timedelta(days=31))
    boundary = await repository.record(**sample, observed_at=clock() - timedelta(days=30))
    engine = build_engine(sessions, clock)
    calls = []
    original = engine._resources.purge

    async def purge(**kwargs):
        calls.append(kwargs["now"])
        return await original(**kwargs)

    monkeypatch.setattr(engine._resources, "purge", purge)
    await engine.start()
    try:
        assert {row.id for row in await repository.range()} >= {boundary}
        assert len(await repository.range()) == 2
        clock.advance(30)
        await engine.sample_resources_once()
        assert len(calls) == 1
        clock.advance(86400 - 30)
        await engine.sample_resources_once()
        assert len(calls) == 2
        assert boundary not in {row.id for row in await repository.range()}
    finally:
        await engine.stop()


async def test_failed_sample_is_not_marked_persisted(sessions, clock, monkeypatch):
    engine = build_engine(sessions, clock)
    original = engine._resources.record

    async def fail(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(engine._resources, "record", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await engine.sample_resources_once()
    assert await ResourceMeasurementRepository(sessions).latest() is None
    monkeypatch.setattr(engine._resources, "record", original)
    assert await engine.sample_resources_once() is True


async def test_orchestr_receives_history_repositories(sessions, clock):
    engine = Engine(
        sessions,
        [stub_for(AgentId.ORCHESTR)],
        Scheduler(SchedulerPolicy()),
        FakeMonitor(),
        clock=clock,
    )
    services = engine.context_for(AgentId.ORCHESTR).services
    assert isinstance(services["resource_measurements"], ResourceMeasurementRepository)
    assert isinstance(services["execution_runs"], ExecutionRunRepository)


def test_settings_wire_resource_intervals(monkeypatch):
    monkeypatch.setenv("AETHER_RESOURCE_SAMPLE_SECONDS", "45")
    monkeypatch.setenv("AETHER_RESOURCE_RETENTION_DAYS", "7")
    monkeypatch.setenv("AETHER_RESOURCE_EVENT_SECONDS", "600")
    limits = EngineLimits.from_settings(AetherSettings(_env_file=None))
    assert limits.resource_sample_seconds == 45
    assert limits.resource_retention_days == 7
    assert limits.resource_event_seconds == 600


@pytest.mark.parametrize(
    "values",
    [
        {"resource_sample_seconds": 0.1},
        {"resource_sample_seconds": float("inf")},
        {"resource_retention_days": 0},
        {"resource_event_seconds": 59},
    ],
)
def test_invalid_settings_are_rejected(values):
    with pytest.raises(ValueError):
        AetherSettings(_env_file=None, **values)
    with pytest.raises(ValueError):
        EngineLimits(**values)
