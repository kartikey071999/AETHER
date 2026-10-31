from datetime import UTC, datetime, timedelta

import pytest

from aether.storage.models import RunStatus, TaskRecord
from aether.storage.repositories import ExecutionRunRepository

NOW = datetime(2030, 1, 1, tzinfo=UTC)


async def test_execution_history_filters_finished_usage(database, objective_id):
    repository = ExecutionRunRepository(database.sessions)
    async with database.sessions() as session, session.begin():
        task = TaskRecord(objective_id=objective_id, title="measured")
        session.add(task)
        await session.flush()
        task_id = task.id
        first = await repository.stage_started(
            session, agent="FORGE", operation="build", task_id=task_id, started_at=NOW
        )
        await repository.stage_finished(
            session,
            first.id,
            status=RunStatus.SUCCEEDED,
            exit_code=0,
            finished_at=NOW + timedelta(seconds=10),
            resource_usage={"duration_seconds": 10, "peak_rss_bytes": 1234},
        )
        second = await repository.stage_started(
            session, agent="SCOUT", operation="fetch", started_at=NOW
        )
        await repository.stage_finished(
            session,
            second.id,
            status=RunStatus.FAILED,
            exit_code=None,
            finished_at=NOW + timedelta(seconds=20),
            resource_usage={"duration_seconds": 20},
        )
        await repository.stage_started(session, agent="FORGE", operation="build", started_at=NOW)
    assert [row.id for row in await repository.history()] == [second.id, first.id]
    assert [row.id for row in await repository.history(limit=1)] == [second.id]
    rows = await repository.history(task_id=task_id, agent="FORGE", operation="build")
    assert [row.id for row in rows] == [first.id]
    assert rows[0].resource_usage == {"duration_seconds": 10, "peak_rss_bytes": 1234}
    assert await repository.history(operation="missing") == []


@pytest.mark.parametrize("limit", [0, 1001])
async def test_execution_history_is_bounded(database, limit):
    with pytest.raises(ValueError):
        await ExecutionRunRepository(database.sessions).history(limit=limit)
