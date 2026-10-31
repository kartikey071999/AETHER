"""Worker agents acknowledge, record, and otherwise ignore work for finished objectives (T-047)."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.agents.base import AgentContext, BaseAgent
from aether.agents.forge import ForgeAgent
from aether.agents.ledger import LedgerAgent
from aether.agents.rogue import RogueAgent
from aether.agents.scout import ScoutAgent
from aether.agents.terminal import IGNORED_DECISION
from aether.agents.warden import WardenAgent
from aether.communication.bus import MessageBus
from aether.communication.contracts import BlockerKind, BlockerPayload, TaskAssignPayload
from aether.communication.contracts import build_message as build
from aether.communication.messages import AgentId
from aether.storage.database import Database
from aether.storage.models import Base, EventRecord
from aether.storage.repositories import ObjectiveRepository, TaskRepository

WORKERS = [ScoutAgent, RogueAgent, ForgeAgent, LedgerAgent, WardenAgent]


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'guard.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


class Spy:
    def __init__(self) -> None:
        self.handled: list = []

    async def __call__(self, message, ctx) -> None:
        self.handled.append(message)


async def _objective(database: Database, status: str):
    objectives = ObjectiveRepository(database.sessions)
    objective_id = await objectives.create(title="Add a text_statistics capability")
    task_id = await TaskRepository(database.sessions).create(
        objective_id=objective_id, title="Do the work", assigned_agent="SCOUT", actor="ORCHESTR"
    )
    if status != "active":
        await objectives.set_status(objective_id, status, actor="ORCHESTR", reason="test")
    return objective_id, task_id


def _assign(agent: type[BaseAgent], objective_id, task_id):
    return build(
        AgentId.ORCHESTR,
        agent.agent_id,
        TaskAssignPayload(
            task_id=task_id,
            objective_id=objective_id,
            title="Do the work",
            assigned_agent=agent.agent_id,
        ),
        objective_id=objective_id,
        task_id=task_id,
    )


async def _ignored(database: Database) -> list[EventRecord]:
    async with database.sessions() as session:
        rows = await session.scalars(
            select(EventRecord).where(EventRecord.event_type == "decision.recorded")
        )
        return [row for row in rows if row.payload.get("decision") == IGNORED_DECISION]


@pytest.mark.parametrize("agent_class", WORKERS)
@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
async def test_work_for_a_terminal_objective_is_ignored(
    database: Database, monkeypatch: pytest.MonkeyPatch, agent_class, status
) -> None:
    objective_id, task_id = await _objective(database, status)
    agent = agent_class()
    spy = Spy()
    monkeypatch.setattr(agent, "handle_message", spy)
    ctx = AgentContext(sessions=database.sessions, bus=MessageBus(database.sessions))
    message = _assign(agent_class, objective_id, task_id)

    await agent.dispatch_message(message, ctx)
    await agent.dispatch_message(message, ctx)  # redelivery records nothing new

    assert spy.handled == []
    [event] = await _ignored(database)
    assert event.objective_id == objective_id
    assert event.actor == agent_class.agent_id.value
    assert event.payload["evidence"] == ["task.assign", "ORCHESTR", status]


@pytest.mark.parametrize("agent_class", WORKERS)
async def test_work_for_an_active_objective_is_handled(
    database: Database, monkeypatch: pytest.MonkeyPatch, agent_class
) -> None:
    objective_id, task_id = await _objective(database, "active")
    agent = agent_class()
    spy = Spy()
    monkeypatch.setattr(agent, "handle_message", spy)
    ctx = AgentContext(sessions=database.sessions, bus=MessageBus(database.sessions))
    await agent.dispatch_message(_assign(agent_class, objective_id, task_id), ctx)
    assert len(spy.handled) == 1
    assert await _ignored(database) == []


async def test_objective_is_resolved_from_the_task_when_the_envelope_lacks_it(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    objective_id, task_id = await _objective(database, "cancelled")
    agent = ScoutAgent()
    spy = Spy()
    monkeypatch.setattr(agent, "handle_message", spy)
    ctx = AgentContext(sessions=database.sessions, bus=MessageBus(database.sessions))
    blocker = build(
        AgentId.FORGE,
        AgentId.SCOUT,
        BlockerPayload(
            blocker_id=uuid4(),
            task_id=task_id,
            kind=BlockerKind.RESEARCH,
            description="need evidence",
            route_to=AgentId.SCOUT,
        ),
    )
    await agent.dispatch_message(blocker, ctx)
    assert spy.handled == []
    assert [event.objective_id for event in await _ignored(database)] == [objective_id]
