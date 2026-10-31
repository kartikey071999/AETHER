"""Read-only per-agent queue views: panel data, CLI and the exposed content boundary."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlite_schema import schema_database, sqlite_url

from aether import main as cli
from aether.agents.queue_view import agent_queue_panel, format_queue_lines
from aether.communication.messages import AgentId
from aether.storage.models import TaskStatus
from aether.storage.repositories import (
    ObjectiveRepository,
    TaskDependencyRepository,
    TaskRepository,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


async def seed(sessions) -> dict[str, UUID]:
    objective = await ObjectiveRepository(sessions).create(title="Queue view objective")
    tasks = TaskRepository(sessions)
    done = await tasks.create(
        objective_id=objective, title="finished work", assigned_agent="FORGE", priority=40
    )
    for step in (TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING, TaskStatus.COMPLETED):
        await tasks.transition(done, step, actor="test")
    cancelled = await tasks.create(
        objective_id=objective, title="dropped work", assigned_agent="FORGE"
    )
    await tasks.transition(cancelled, TaskStatus.CANCELLED, actor="test")
    urgent = await tasks.create(
        objective_id=objective,
        title="urgent work",
        description="SECRET-DESCRIPTION",
        assigned_agent="FORGE",
        priority=90,
    )
    waiting = await tasks.create(
        objective_id=objective, title="waiting work", assigned_agent="FORGE", priority=10
    )
    other = await tasks.create(objective_id=objective, title="scout work", assigned_agent="SCOUT")
    for task_id in (urgent, waiting, other):
        await tasks.transition(task_id, TaskStatus.READY, actor="test")
    await TaskDependencyRepository(sessions).add(waiting, other, actor="test")
    return {"done": done, "cancelled": cancelled, "urgent": urgent, "waiting": waiting}


@pytest.fixture
def seeded_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, UUID]:
    path = tmp_path / "queue.db"
    database = schema_database(path)
    ids = asyncio.run(seed(database.sessions))
    asyncio.run(database.close())
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", sqlite_url(path))
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AETHER_LOG_DIR", str(tmp_path / "logs"))
    return ids


async def test_panel_lists_open_tasks_by_priority_without_content(tmp_path: Path) -> None:
    database = schema_database(tmp_path / "panel.db")
    try:
        ids = await seed(database.sessions)
        panel = await agent_queue_panel(database.sessions, AgentId.FORGE, clock=lambda: NOW)
    finally:
        await database.close()

    assert panel["agent"] == "FORGE" and panel["view"] == "open" and panel["count"] == 2
    assert [item["id"] for item in panel["items"]] == [ids["urgent"], ids["waiting"]]
    waiting = panel["items"][1]
    assert waiting["ready_to_run"] is False and len(waiting["blocked_by"]) == 1
    assert "SECRET-DESCRIPTION" not in str(panel)
    assert not {"description", "result", "last_error"} & set(panel["items"][0])


async def test_panel_finished_is_opt_in_and_includes_cancelled(tmp_path: Path) -> None:
    database = schema_database(tmp_path / "finished.db")
    try:
        ids = await seed(database.sessions)
        panel = await agent_queue_panel(
            database.sessions, AgentId.FORGE, finished=True, clock=lambda: NOW
        )
        scout = await agent_queue_panel(database.sessions, AgentId.SCOUT, finished=True)
    finally:
        await database.close()

    assert panel["view"] == "finished"
    assert {item["id"] for item in panel["items"]} == {ids["done"], ids["cancelled"]}
    assert scout["items"] == []


def test_cli_queue_prints_open_tasks_for_one_agent(seeded_database, capsys) -> None:
    cli.main(["agents", "queue", "forge"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "FORGE open queue: 2 task(s)"
    assert "urgent work" in lines[1] and "ready" in lines[1]
    assert "waiting work" in lines[2] and "blocked_by=1" in lines[2]
    assert not any("finished work" in line or "scout work" in line for line in lines)


def test_cli_queue_finished_flag_shows_finished_tasks(seeded_database, capsys) -> None:
    cli.main(["agents", "queue", "FORGE", "--finished", "--limit", "5"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "FORGE finished queue: 2 task(s)"
    assert any("finished work" in line for line in lines)
    assert any("dropped work" in line for line in lines)
    assert not any("urgent work" in line for line in lines)


def test_cli_queue_rejects_unknown_agents_and_agents_still_lists(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["agents", "queue", "nobody"])
    assert exited.value.code == 2
    assert "unknown agent" in capsys.readouterr().err

    cli.main(["agents"])
    assert "FORGE:" in capsys.readouterr().out


def test_cli_queue_failure_reports_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AETHER_DATABASE_URL", sqlite_url(tmp_path / "missing" / "none.db"))
    with pytest.raises(SystemExit) as exited:
        cli.main(["agents", "queue", "FORGE"])
    assert exited.value.code == 1
    assert "Reading the queue failed" in capsys.readouterr().err


def test_format_queue_lines_handles_an_empty_queue() -> None:
    panel = {"agent": "MUSE", "view": "open", "count": 0, "items": []}
    assert format_queue_lines(panel) == ["MUSE open queue: 0 task(s)"]
