"""Agent-authored workflows: propose -> (WARDEN) approve -> (LEDGER) dry-run verify -> run."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlite_schema import schema_database

from aether.agents.base import AgentContext
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.security.untrusted import UntrustedContent
from aether.skills import SkillRegistry, builtin_skills
from aether.storage.models import EventRecord, WorkflowStatus
from aether.workflows import RESEARCH_BASIC, WorkflowDefinitionError, WorkflowEngine
from aether.workflows.facade import AgentWorkflows, WorkflowNotPermitted
from aether.workflows.lifecycle import WorkflowLifecycleError

URL = "https://docs.python.org/3/library/stdtypes.html"
TEXT = "str.split() splits a string on whitespace; len() counts the words."
HASH = UntrustedContent(source=URL, content=TEXT).content_hash

GATHER_AND_SCREEN = {
    "name": "scout.gather_and_screen",
    "description": "Fetch pages and drop any with injected instructions.",
    "inputs": {"urls": "list"},
    "steps": [
        {"id": "gather", "skill": "web.gather", "inputs": {"urls": "${input.urls}"}},
        {
            "id": "screen",
            "skill": "text.screen_untrusted",
            "inputs": {"sources": "${steps.gather.output.sources}"},
        },
    ],
    "outputs": {"clean": "${steps.screen.output.clean}"},
}
SCREEN_ONLY = {
    "name": "scout.screen_only",
    "inputs": {"sources": "list"},
    "steps": [
        {
            "id": "screen",
            "skill": "text.screen_untrusted",
            "inputs": {"sources": "${input.sources}"},
        }
    ],
    "outputs": {"clean": "${steps.screen.output.clean}"},
}
FIXTURES = {
    "web.gather": {"sources": [{"url": URL, "text": TEXT, "content_hash": HASH}], "failed": []}
}


@pytest.fixture
async def database(tmp_path: Path):
    database = schema_database(tmp_path / "lifecycle.db")
    yield database
    await database.close()


@pytest.fixture
def engine(database):
    return WorkflowEngine(database.sessions, SkillRegistry(builtin_skills()))


def facade(engine, agent):
    return AgentWorkflows(agent, engine)


def agent_ctx(database, tools=None):
    return AgentContext(sessions=database.sessions, bus=MessageBus(database.sessions), tools=tools)


class FakeTools:
    def __init__(self):
        self.calls = []

    async def invoke(self, tool, args, ctx=None):
        self.calls.append(args["url"])
        return SimpleNamespace(
            final_url=args["url"], title="", body=UntrustedContent(source=args["url"], content=TEXT)
        )


async def test_side_effecting_proposal_waits_for_warden_and_pure_one_is_auto_approved(engine):
    scout = facade(engine, AgentId.SCOUT)
    assert await scout.propose(GATHER_AND_SCREEN, "reuse for every research task") == (
        "scout.gather_and_screen",
        1,
        "proposed",
    )
    assert await scout.propose(SCREEN_ONLY, "deterministic screening") == (
        "scout.screen_only",
        1,
        "approved",
    )


@pytest.mark.parametrize(
    ("change", "error", "match"),
    [
        ({"name": "rogue.sneaky"}, WorkflowLifecycleError, "must start with 'scout.'"),
        ({"owner": "ROGUE"}, WorkflowLifecycleError, "only propose its own"),
        (
            {"steps": [{"id": "x", "skill": "nope"}], "outputs": {}},
            WorkflowDefinitionError,
            "unknown skill",
        ),
        (
            {
                "steps": [
                    {
                        "id": "x",
                        "skill": "llm.summarise",
                        "inputs": {"question": "${steps.y.output.q}"},
                    }
                ]
            },
            WorkflowDefinitionError,
            # The message names the actual problem so the authoring agent can fix it.
            "invalid workflow: .*invalid reference",
        ),
    ],
)
async def test_invalid_proposals_are_rejected(engine, change, error, match):
    with pytest.raises(error, match=match):
        await facade(engine, AgentId.SCOUT).propose({**GATHER_AND_SCREEN, **change}, "why")


async def test_agents_cannot_author_workflows_with_skills_they_do_not_own(engine):
    forge_flow = {
        "name": "forge.fetch",
        "steps": [{"id": "g", "skill": "web.gather", "inputs": {"urls": [URL]}}],
    }
    with pytest.raises(WorkflowDefinitionError, match="may not use skill"):
        await facade(engine, AgentId.FORGE).propose(forge_flow, "why")


async def test_a_rationale_is_required(engine):
    with pytest.raises(WorkflowDefinitionError, match="rationale"):
        await facade(engine, AgentId.SCOUT).propose(GATHER_AND_SCREEN, "  ")


async def test_reproposing_a_name_creates_a_new_version(engine):
    scout = facade(engine, AgentId.SCOUT)
    await scout.propose(SCREEN_ONLY, "v1")
    changed = {**SCREEN_ONLY, "description": "second take"}
    assert await scout.propose(changed, "v2") == ("scout.screen_only", 2, "approved")


async def test_full_lifecycle_until_the_workflow_runs(engine, database):
    scout, warden, ledger = (
        facade(engine, a) for a in (AgentId.SCOUT, AgentId.WARDEN, AgentId.LEDGER)
    )
    name, version, _ = await scout.propose(GATHER_AND_SCREEN, "reuse")
    with pytest.raises(WorkflowLifecycleError, match="only WARDEN"):
        await scout.approve(name, version, "self-approval")
    with pytest.raises(WorkflowNotPermitted, match="not verified"):
        await scout.run(name, {"urls": [URL]}, agent_ctx(database))
    await warden.approve(name, version, "allowlisted reads only")
    with pytest.raises(WorkflowLifecycleError, match="only LEDGER"):
        await warden.verify(
            name, version, agent_ctx(database), inputs={"urls": [URL]}, fixtures=FIXTURES
        )
    tools = FakeTools()
    assert await ledger.verify(
        name, version, agent_ctx(database, tools), inputs={"urls": [URL]}, fixtures=FIXTURES
    )
    assert tools.calls == []  # the dry run caused no side effects
    result = await scout.run(name, {"urls": [URL]}, agent_ctx(database, tools))
    assert result.status == "succeeded" and tools.calls == [URL]
    async with database.sessions() as session:
        changes = [
            e.payload
            for e in await session.scalars(
                select(EventRecord)
                .where(EventRecord.event_type == "workflow.definition_status_changed")
                .order_by(EventRecord.created_at)
            )
        ]
    assert [(c["from_status"], c["to_status"], c["actor"]) for c in changes] == [
        (None, "proposed", "SCOUT"),
        ("proposed", "approved", "WARDEN"),
        ("approved", "verified", "LEDGER"),
    ]


async def test_failing_dry_run_rejects_the_workflow_with_a_reason(engine, database):
    scout, warden, ledger = (
        facade(engine, a) for a in (AgentId.SCOUT, AgentId.WARDEN, AgentId.LEDGER)
    )
    name, version, _ = await scout.propose(GATHER_AND_SCREEN, "reuse")
    await warden.approve(name, version, "ok")
    assert not await ledger.verify(
        name, version, agent_ctx(database), inputs={"urls": [URL]}, fixtures={}
    )
    _, status = await engine.load(name, version)
    assert status == WorkflowStatus.REJECTED
    with pytest.raises(WorkflowLifecycleError, match="cannot move"):
        await warden.approve(name, version, "try again")


async def test_rejection_needs_a_reason_and_the_right_role(engine):
    scout = facade(engine, AgentId.SCOUT)
    name, version, _ = await scout.propose(GATHER_AND_SCREEN, "reuse")
    with pytest.raises(WorkflowLifecycleError, match="only WARDEN or LEDGER"):
        await scout.reject(name, version, "no")
    with pytest.raises(WorkflowDefinitionError, match="reason"):
        await facade(engine, AgentId.WARDEN).reject(name, version, " ")


async def test_built_in_workflows_cannot_be_changed(engine):
    await engine.register(
        RESEARCH_BASIC, status=WorkflowStatus.VERIFIED, built_in=True, created_by="RUNTIME"
    )
    with pytest.raises(WorkflowLifecycleError, match="built-in"):
        await facade(engine, AgentId.WARDEN).disable("research.basic", 1, "no")


async def test_owner_can_disable_a_verified_workflow(engine, database):
    scout, ledger = facade(engine, AgentId.SCOUT), facade(engine, AgentId.LEDGER)
    name, version, status = await scout.propose(SCREEN_ONLY, "deterministic")
    assert status == "approved"
    assert await ledger.verify(
        name, version, agent_ctx(database), inputs={"sources": []}, fixtures={}
    )
    await scout.disable(name, version, "superseded")
    with pytest.raises(WorkflowNotPermitted, match="disabled"):
        await scout.run(name, {"sources": []}, agent_ctx(database))
