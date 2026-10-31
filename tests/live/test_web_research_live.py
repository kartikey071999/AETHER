"""One opt-in pypi.package call through the full web research security path."""

import os

import pytest

from aether.communication.messages import AgentId
from aether.observability.recorder import EventRecorder
from aether.security import PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import Base
from aether.tools import ToolRegistry
from aether.tools.web import SafeWeb, WebSettings, register_web_tools


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("AETHER_LIVE_TESTS") != "1", reason="live calls are explicitly opt-in"
)
async def test_pypi_package_json_page(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'live-web.db').as_posix()}")
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        recorder = EventRecorder()
        permissions = PermissionService(
            database.sessions,
            recorder,
            SecurityPolicy(
                workspace_dir=tmp_path,
                artifact_dir=tmp_path,
                data_dir=tmp_path,
                web_allowed_hosts=["pypi.org"],
            ),
        )
        registry = ToolRegistry(permissions)
        register_web_tools(
            registry,
            SafeWeb(
                permissions,
                database.sessions,
                recorder,
                WebSettings(_env_file=None, allowed_hosts=["pypi.org"]),
            ),
        )
        result = await registry.invoke(AgentId.SCOUT, "pypi.package", {"name": "httpx"})
        assert result.status_code == 200 and not result.from_cache
        assert result.facts["name"].lower() == "httpx" and result.facts["version"]
        assert result.body.source == "https://pypi.org/pypi/httpx/json"
        assert len(result.raw_sha256) == 64
    finally:
        await database.close()
