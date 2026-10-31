"""SCOUT and ROGUE researching through the real web tools against an offline mock internet."""

import json
import sys
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from web_fixtures import DOCS_HOST, WEB_HOSTS, MockWeb, logical_host, public_resolver

from aether.agents.base import AgentContext, TaskResultStatus
from aether.agents.rogue import RogueAgent
from aether.agents.scout import ScoutAgent
from aether.agents.scout.schemas import ResearchLimits
from aether.communication.contracts import (
    ResearchBudget,
    ResearchMode,
    ResearchRequestPayload,
    build_message,
)
from aether.communication.messages import AgentId
from aether.observability.recorder import EventRecorder
from aether.observability.spool import EventSpool
from aether.security import PermissionService, SecurityPolicy
from aether.storage.database import Database
from aether.storage.models import (
    FindingClassification,
    FindingEvidenceRecord,
    ResearchFindingRecord,
    TaskRecord,
)
from aether.tools import AgentTools, ToolRegistry
from aether.tools.web import SafeWeb, WebSettings, register_web_tools

DOCS = f"https://{DOCS_HOST}/en/latest/"
ARTICLE = f"https://{DOCS_HOST}/guide/article.html"


@dataclass
class WebEnv:
    web: MockWeb
    registry: ToolRegistry
    sleeps: list[float]


@pytest.fixture
def web_env(database: Database, tmp_path: Path) -> WebEnv:
    roots = {name: tmp_path / name for name in ("workspace", "artifacts", "data")}
    for path in roots.values():
        path.mkdir()
    policy = SecurityPolicy(
        workspace_dir=roots["workspace"],
        artifact_dir=roots["artifacts"],
        data_dir=roots["data"],
        allowed_executables=[sys.executable],
        allowed_hosts=[],
        web_allowed_hosts=list(WEB_HOSTS),
        max_approval_ttl=timedelta(hours=1),
        authorization_ttl=timedelta(seconds=60),
        max_response_bytes=1024 * 1024,
    )
    recorder = EventRecorder(EventSpool(tmp_path / "spool" / "events.jsonl"))
    permissions = PermissionService(database.sessions, recorder, policy)
    web, sleeps = MockWeb(), []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter = SafeWeb(
        permissions,
        database.sessions,
        recorder,
        WebSettings(
            _env_file=None,
            allowed_hosts=list(WEB_HOSTS),
            docs_hosts=[DOCS_HOST],
        ),
        transport=web.transport,
        resolver=public_resolver,
        sleep=fake_sleep,
    )
    registry = ToolRegistry(permissions)
    register_web_tools(registry, adapter)
    return WebEnv(web, registry, sleeps)


def request(objective_id, agent=AgentId.SCOUT, *, budget=4, scope="", question=None):
    payload = ResearchRequestPayload(
        objective_id=objective_id,
        question=question or "How should AETHER stream large HTTP downloads?",
        mode=ResearchMode.UNCONVENTIONAL if agent is AgentId.ROGUE else ResearchMode.CONVENTIONAL,
        budget=ResearchBudget(max_sources=budget, max_llm_requests=budget),
        scope=scope,
    )
    return build_message(AgentId.ORCHESTR, agent, payload, objective_id=objective_id)


def scope(*sources: dict[str, str]) -> str:
    return json.dumps({"sources": list(sources)})


async def records(context: AgentContext, model):
    async with context.sessions() as session:
        return list(await session.scalars(select(model)))


async def run(agent, message, ctx: AgentContext):
    await agent.handle_message(message, ctx)
    async with ctx.sessions() as session:
        task = await session.scalar(
            select(TaskRecord).where(TaskRecord.assigned_agent == agent.agent_id.value)
        )
    return await agent.execute_task(task, ctx)


async def test_scout_cites_web_pypi_and_docs_evidence_with_hashes(
    context: AgentContext, objective_id, web_env: WebEnv
) -> None:
    ctx = replace(context, tools=AgentTools(AgentId.SCOUT, web_env.registry))
    message = request(
        objective_id,
        scope=scope(
            {"kind": "web", "location": ARTICLE, "purpose": "Read the streaming guide"},
            {"kind": "pypi", "location": "example-http", "purpose": "Check package metadata"},
            {"kind": "docs", "location": DOCS, "query": "iter bytes", "purpose": "API reference"},
        ),
    )
    result = await run(ScoutAgent(), message, ctx)

    assert result.status is TaskResultStatus.COMPLETED
    [finding] = await records(context, ResearchFindingRecord)
    evidence = await records(context, FindingEvidenceRecord)
    assert len(evidence) == 3
    # Each citation is exactly the untrusted text the tool returned (re-read from the cache).
    expected = {}
    for tool, args in (
        ("web.extract", {"url": ARTICLE}),
        ("pypi.package", {"name": "example-http"}),
        ("docs.search", {"base_url": DOCS, "query": "iter bytes"}),
    ):
        again = await web_env.registry.invoke(AgentId.SCOUT, tool, args)
        assert again.from_cache
        expected[again.body.source] = again.body.content_hash
    assert {item.source_uri: item.content_hash for item in evidence} == expected
    assert ARTICLE in expected and "https://pypi.org/pypi/example-http/json" in expected
    assert finding.objective_id == objective_id
    hosts = {logical_host(item) for item in web_env.web.requests}
    assert hosts == {DOCS_HOST, "pypi.org"}
    assert "/robots.txt" in web_env.web.paths(DOCS_HOST)
    assert web_env.web.paths("pypi.org") == ["/pypi/example-http/json"]


async def test_rogue_derives_github_search_and_follows_up_on_a_repository(
    context: AgentContext, objective_id, web_env: WebEnv
) -> None:
    ctx = replace(context, tools=AgentTools(AgentId.ROGUE, web_env.registry))
    limits = ResearchLimits(web_research=True, docs_base_url=DOCS)
    result = await run(RogueAgent(limits), request(objective_id, AgentId.ROGUE, budget=3), ctx)

    assert result.status is TaskResultStatus.COMPLETED
    github = web_env.web.paths("api.github.com")
    # ROGUE reverses the derived order: repository search first, then the follow-up repo.
    assert github[0] == "/search/repositories"
    assert "/repos/example/http" in github
    search = next(r for r in web_env.web.requests if r.url.path == "/search/repositories")
    assert search.url.params["q"] == "aether stream large http downloads"
    evidence = await records(context, FindingEvidenceRecord)
    uris = {item.source_uri for item in evidence}
    assert "https://api.github.com/repos/example/http" in uris
    assert len(evidence) == 3


async def test_web_research_is_opt_in(context: AgentContext, objective_id, web_env: WebEnv) -> None:
    ctx = replace(context, tools=AgentTools(AgentId.SCOUT, web_env.registry))
    result = await run(ScoutAgent(), request(objective_id), ctx)
    assert result.status is TaskResultStatus.COMPLETED
    assert web_env.web.requests == []


async def test_injected_page_is_quarantined_not_obeyed(
    context: AgentContext, objective_id, web_env: WebEnv, models
) -> None:
    facade, provider = models(AgentId.SCOUT, [])
    ctx = replace(context, tools=AgentTools(AgentId.SCOUT, web_env.registry), models=facade)
    injected = f"https://{DOCS_HOST}/injection.html"
    message = request(objective_id, scope=scope({"kind": "web", "location": injected}))
    result = await run(ScoutAgent(), message, ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert provider.requests == []
    [finding] = await records(context, ResearchFindingRecord)
    assert finding.classification == FindingClassification.UNTESTED
    assert finding.confidence == 0.2
    hosts = {logical_host(item) for item in web_env.web.requests}
    assert hosts == {DOCS_HOST}


async def test_unavailable_web_sources_are_skipped_not_fatal(
    context: AgentContext, objective_id, web_env: WebEnv
) -> None:
    ctx = replace(context, tools=AgentTools(AgentId.SCOUT, web_env.registry))
    message = request(
        objective_id,
        scope=scope(
            {"kind": "web", "location": f"https://{DOCS_HOST}/private/internal.html"},
            {"kind": "web", "location": "https://evil.example.net/page"},
            {"kind": "pypi", "location": "no-such-package"},
            {"kind": "pypi", "location": "example-http"},
        ),
    )
    result = await run(ScoutAgent(), message, ctx)

    assert result.status is TaskResultStatus.COMPLETED
    evidence = await records(context, FindingEvidenceRecord)
    assert [item.source_uri for item in evidence] == ["https://pypi.org/pypi/example-http/json"]
    assert "/private/internal.html" not in web_env.web.paths(DOCS_HOST)
    assert all(logical_host(item) != "evil.example.net" for item in web_env.web.requests)
