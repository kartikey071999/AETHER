"""Skills and workflows through the real runtime: injection, built-in registration, ownership."""

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlite_schema import copy_schema_to

from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.models.settings import ProviderSettings
from aether.runtime.bootstrap import Runtime, build_runtime
from aether.security.untrusted import UntrustedContent
from aether.storage.models import WorkflowDefinitionRecord, WorkflowStatus
from aether.workflows import WorkflowDefinition, WorkflowStep
from aether.workflows.facade import WorkflowNotPermitted

URL = "https://docs.python.org/3/library/stdtypes.html"


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invoke(self, tool, args, ctx=None):
        self.calls.append(args["url"])
        body = UntrustedContent(source=args["url"], content="str.split() splits words on spaces.")
        return SimpleNamespace(final_url=args["url"], title="", body=body)


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[Runtime]:
    settings = AetherSettings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'wf.db').as_posix()}",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        artifact_dir=tmp_path / "artifacts",
        workspace_dir=tmp_path / "workspaces",
    )
    runtime = build_runtime(
        settings, provider_settings=ProviderSettings(_env_file=None, mock=True, max_retries=0)
    )
    copy_schema_to(settings.database_url.get_secret_value())
    await runtime.engine.start()
    try:
        yield runtime
    finally:
        await runtime.engine.stop()
        await runtime.close()


async def test_each_agent_context_has_only_its_own_skills(runtime: Runtime) -> None:
    scout = runtime.engine.context_for(AgentId.SCOUT)
    assert scout.skills.names() == (
        "llm.structured",
        "llm.summarise",
        "text.screen_untrusted",
        "web.gather",
    )
    assert runtime.engine.context_for(AgentId.WARDEN).skills.names() == (
        "llm.structured",
        "text.screen_untrusted",
    )
    assert runtime.engine.context_for(AgentId.FORGE).skills.names() == ("llm.structured",)
    assert scout.workflows.agent_id is AgentId.SCOUT


async def test_builtin_workflows_are_registered_verified_on_start(runtime: Runtime) -> None:
    async with runtime.database.sessions() as session:
        rows = list(await session.scalars(select(WorkflowDefinitionRecord)))
    assert [(r.name, r.status, r.built_in) for r in rows] == [("research.basic", "verified", True)]


async def test_scout_runs_the_builtin_research_workflow_through_its_context(
    runtime: Runtime,
) -> None:
    tools = FakeTools()
    ctx = replace(runtime.engine.context_for(AgentId.SCOUT), tools=tools)
    result = await ctx.workflows.run(
        "research.basic", {"question": "How are words split?", "urls": [URL]}, ctx
    )
    assert result.status == "succeeded" and tools.calls == [URL]
    assert result.outputs["cited_hashes"]


async def test_other_agents_cannot_run_scouts_workflow(runtime: Runtime) -> None:
    ctx = runtime.engine.context_for(AgentId.ROGUE)
    with pytest.raises(WorkflowNotPermitted, match="does not own"):
        await ctx.workflows.run("research.basic", {"question": "q", "urls": [URL]}, ctx)


async def test_a_proposed_workflow_cannot_run_until_approved(runtime: Runtime) -> None:
    proposal = WorkflowDefinition(
        name="scout.screen_only",
        owner=AgentId.SCOUT,
        steps=[WorkflowStep(id="screen", skill="text.screen_untrusted", inputs={"sources": []})],
    )
    await runtime.workflows.register(
        proposal, status=WorkflowStatus.PROPOSED, built_in=False, created_by="SCOUT"
    )
    ctx = runtime.engine.context_for(AgentId.SCOUT)
    with pytest.raises(WorkflowNotPermitted, match="proposed"):
        await ctx.workflows.run("scout.screen_only", {}, ctx)
