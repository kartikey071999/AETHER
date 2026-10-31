import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine

from aether.agents.base import AgentContext
from aether.agents.muse import MuseAgent, MuseSettings
from aether.communication.bus import MessageBus
from aether.communication.contracts import CommunicationRequestPayload, build_message
from aether.communication.messages import AgentId
from aether.observability.recorder import EventRecorder
from aether.security.operations import OperationKind
from aether.security.permissions import PermissionService
from aether.security.policy import SecurityPolicy
from aether.security.readonly import PermissionView
from aether.storage.database import Database
from aether.storage.models import Base
from aether.storage.repositories import ObjectiveRepository, PersonRepository, TaskRepository
from aether.tools.adapters import build_default_registry
from aether.tools.facade import AgentTools


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def database(tmp_path, muse_schema) -> AsyncIterator[Database]:
    path = tmp_path / "muse.db"
    shutil.copyfile(muse_schema, path)
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    yield database
    await database.close()


@pytest.fixture(scope="session")
def muse_schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("muse-schema") / "empty.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def policy(tmp_path):
    roots = {name: tmp_path / name for name in ("workspaces", "artifacts", "data")}
    for path in roots.values():
        path.mkdir()
    # Explicit startup permission in this isolated test only; production remains default-deny.
    return SecurityPolicy(
        workspace_dir=roots["workspaces"],
        artifact_dir=roots["artifacts"],
        data_dir=roots["data"],
        agent_permissions={AgentId.MUSE: {OperationKind.FILE_WRITE, OperationKind.LLM_CALL}},
    )


@pytest.fixture
def permissions(database, policy, clock):
    return PermissionService(database.sessions, EventRecorder(), policy, clock=clock)


@pytest.fixture
def ctx(database, permissions, clock):
    registry = build_default_registry(permissions, database.sessions, EventRecorder())
    return AgentContext(
        sessions=database.sessions,
        bus=MessageBus(database.sessions, clock=clock),
        clock=clock,
        permission_service=PermissionView(permissions),
        tools=AgentTools(AgentId.MUSE, registry),
        services={"muse_settings": MuseSettings(data_dir=permissions.policy.data_dir)},
    )


@pytest.fixture
def agent():
    return MuseAgent()


@pytest.fixture
async def person_id(database):
    return await PersonRepository(database.sessions).create(
        display_name="Owner", access_level="full"
    )


@pytest.fixture
async def objective_id(database):
    return await ObjectiveRepository(database.sessions).create(title="Communication objective")


@pytest.fixture
async def task_id(database, objective_id):
    return await TaskRepository(database.sessions).create(
        objective_id=objective_id, title="Communicate results", assigned_agent="MUSE"
    )


@pytest.fixture
def request_message(person_id, objective_id, task_id):
    def make(**changes):
        envelope = changes.pop("envelope", {})
        values = {
            "what_happened": "Completed the requested evaluation.",
            "results": [{"tests": 12, "status": "reported"}],
            "verified": ["12 tests passed."],
            "uncertain": ["Performance has not been measured."],
            "blockers": ["Waiting for hardware."],
            "audience": person_id,
            "objective_id": objective_id,
            "task_id": task_id,
        }
        values.update(changes)
        return build_message(
            AgentId.ORCHESTR,
            AgentId.MUSE,
            CommunicationRequestPayload(**values),
            objective_id=objective_id,
            task_id=task_id,
            **envelope,
        )

    return make
