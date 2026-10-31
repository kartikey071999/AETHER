import socket
import sys
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from forge_support import FakeClock, ForgeEnv

from aether.agents.base import AgentContext
from aether.agents.forge import ForgeAgent
from aether.agents.forge.schemas import ForgeConfig
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.models.contracts import CompletionRequest, CompletionResponse
from aether.models.facade import AgentModels
from aether.observability.recorder import EventRecorder
from aether.observability.spool import EventSpool
from aether.security import ApprovalService, PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import Base
from aether.tools import AgentTools, ToolRegistry
from aether.tools.adapters import build_default_registry


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'forge.db').as_posix()}")
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.close()


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    paths = {name: tmp_path / name for name in ("workspace", "artifacts", "data")}
    for path in paths.values():
        path.mkdir()
    return paths


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def policy(roots: dict[str, Path]) -> SecurityPolicy:
    return SecurityPolicy(
        workspace_dir=roots["workspace"],
        artifact_dir=roots["artifacts"],
        data_dir=roots["data"],
        allowed_executables=[sys.executable],
        allowed_hosts=["api.example.com"],
        max_approval_ttl=timedelta(hours=1),
        authorization_ttl=timedelta(seconds=60),
        subprocess_timeout_seconds=30,
        max_output_bytes=4096,
    )


@pytest.fixture
def recorder(tmp_path: Path) -> EventRecorder:
    return EventRecorder(EventSpool(tmp_path / "spool" / "events.jsonl"))


@pytest.fixture
def registry(
    database: Database, recorder: EventRecorder, policy: SecurityPolicy, clock: FakeClock
) -> ToolRegistry:
    permissions = PermissionService(database.sessions, recorder, policy, clock=clock)
    return build_default_registry(
        permissions,
        database.sessions,
        recorder,
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        http_resolver=lambda host, port: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ],
    )


@pytest.fixture
def make_env(
    database: Database,
    roots: dict[str, Path],
    clock: FakeClock,
    recorder: EventRecorder,
    policy: SecurityPolicy,
    registry: ToolRegistry,
) -> Callable[..., ForgeEnv]:
    def build(config: ForgeConfig | None = None) -> ForgeEnv:

        sleeps: list[float] = []
        requests: list[CompletionRequest] = []
        replies: list[str | Exception] = []

        async def complete(request: CompletionRequest) -> CompletionResponse:
            requests.append(request)
            if not replies:
                raise RuntimeError("no reply configured")
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return CompletionResponse(
                text=reply,
                model="mock-model",
                provider="mock",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1.0,
            )

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        ctx = AgentContext(
            sessions=database.sessions,
            bus=MessageBus(database.sessions),
            clock=clock,
            tools=AgentTools(AgentId.FORGE, registry),
            models=AgentModels(AgentId.FORGE, complete),
        )
        return ForgeEnv(
            database=database,
            ctx=ctx,
            agent=ForgeAgent(config or ForgeConfig(), sleep=fake_sleep),
            clock=clock,
            approvals=ApprovalService(database.sessions, recorder, policy, clock=clock),
            roots=roots,
            sleeps=sleeps,
            llm_requests=requests,
            llm_replies=replies,
            config=config or ForgeConfig(),
        )

    return build


@pytest.fixture
def env(make_env: Callable[..., ForgeEnv]) -> ForgeEnv:
    return make_env()
