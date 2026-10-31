from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.communication.bus import MessageBus
from aether.models.facade import AgentModels
from aether.models.providers import MockProvider
from aether.security.operations import Operation, OperationKind
from aether.security.untrusted import UntrustedContent
from aether.storage.database import Database
from aether.storage.repositories import ObjectiveRepository


@dataclass
class FakeTools:
    content: str = "A local implementation uses bounded buffers and supports offline operation."
    calls: list[tuple[str, dict]] = field(default_factory=list)
    fail: Exception | None = None

    async def invoke(self, tool, args, call):
        self.calls.append((tool, args))
        if self.fail:
            raise self.fail
        return UntrustedContent(
            source=args.get("url", f"file:{args.get('path')}"), content=self.content
        )

    def build_operation(self, tool, args, call):
        return Operation(
            kind=OperationKind.HTTP_REQUEST if tool == "http_request" else OperationKind.FILE_READ,
            target=args.get("url", args.get("path")),
            arguments=args,
            requested_by="SCOUT",
            task_id=call.task_id,
            objective_id=call.objective_id,
        )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = schema_database(tmp_path / "research.db")
    yield database
    await database.close()


@pytest.fixture
async def objective_id(database: Database) -> UUID:
    return await ObjectiveRepository(database.sessions).create(title="Local voice presence")


@pytest.fixture
def context(database: Database) -> AgentContext:
    return AgentContext(sessions=database.sessions, bus=MessageBus(database.sessions))


@pytest.fixture
def fake_tools() -> FakeTools:
    return FakeTools()


@pytest.fixture
def models():
    def build(agent_id, responses):
        provider = MockProvider(responses)
        return AgentModels(agent_id, provider.complete), provider

    return build
