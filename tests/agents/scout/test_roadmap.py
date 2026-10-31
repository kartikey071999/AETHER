import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from aether.agents.scout import ScoutAgent
from aether.communication.contracts import ResearchFindingPayload, build_message
from aether.communication.messages import AgentId
from aether.storage.models import CapabilityRecord, CapabilityStatus, EventRecord
from aether.storage.repositories import (
    CapabilityRepository,
    ObjectiveRepository,
    ResearchFindingRepository,
)
from aether.storage.repositories.research import EvidenceInput


async def finding_message(
    context, objective_id, name, *, confidence=0.1, alternatives=None, **candidate_fields
):
    repository = ResearchFindingRepository(context.sessions)
    finding_id = await repository.submit(
        agent="ROGUE",
        objective_id=objective_id,
        question="Compare approaches",
        findings="A reported approach",
        confidence=confidence,
        classification="hypothesis",
        candidate_approaches=[*(alternatives or []), {"name": name, **candidate_fields}],
        recommendation=name,
        evidence=[
            EvidenceInput(
                source_uri=f"file:source-{uuid4()}",
                summary="Local reference",
                content_hash="a" * 64,
            )
        ],
    )
    return build_message(
        AgentId.ROGUE,
        AgentId.SCOUT,
        ResearchFindingPayload(
            finding_id=finding_id,
            objective_id=objective_id,
            question="Compare approaches",
            summary="A reported approach",
            confidence=confidence,
            classification="hypothesis",
            submitted_by=AgentId.ROGUE,
        ),
    )


async def capabilities(context):
    async with context.sessions() as session:
        return list(await session.scalars(select(CapabilityRecord)))


async def capability_events(context):
    async with context.sessions() as session:
        return list(
            await session.scalars(select(EventRecord).where(EventRecord.category == "capability"))
        )


async def test_rounds_merge_discoveries_and_preserve_evidence_links(context, objective_id):
    messages = [
        await finding_message(
            context,
            objective_id,
            name,
            description=f"Description {index}",
            dependencies=["common", f"dependency-{index}"],
            cons=["Untested", f"Limitation {index}"],
            resource_needs={"ram_mb": index + 1},
        )
        for index, name in enumerate(["Regex-tokenizer", " regex-TOKENIZER ", "REGEX-tokenizer"])
    ]
    for message in messages:
        await ScoutAgent().handle_message(message, context)
    [capability] = await capabilities(context)
    assert capability.lifecycle_status == "discovered"
    assert capability.description == "Description 2"
    assert capability.dependencies == ["common", "dependency-0", "dependency-1", "dependency-2"]
    assert capability.known_limitations == [
        "Untested",
        "Limitation 0",
        "Limitation 1",
        "Limitation 2",
    ]
    assert capability.resource_estimates["ram_mb"] == 3
    events = await capability_events(context)
    assert len([event for event in events if event.event_type == "capability.discovered"]) == 1
    additions = [event for event in events if event.event_type == "capability.evidence_added"]
    assert len(additions) == 3
    repository = ResearchFindingRepository(context.sessions)
    for message in messages:
        finding_id = message.payload["finding_id"]
        [addition] = [event for event in additions if event.payload["finding_id"] == finding_id]
        evidence = await repository.list_evidence(UUID(finding_id))
        assert addition.aggregate_id == capability.id
        assert addition.objective_id == objective_id
        assert addition.correlation_id == message.correlation_id
        assert addition.payload["evidence_ids"] == [str(row.id) for row in evidence]
    await ScoutAgent().handle_message(messages[-1], context)
    assert len(await capabilities(context)) == 1
    assert len(await capability_events(context)) == len(events)


async def test_concurrent_findings_share_one_row_but_not_across_objectives(context, objective_id):
    other_id = await ObjectiveRepository(context.sessions).create(title="Other objective")
    messages = [
        await finding_message(context, objective_id, name)
        for name in ["Local gate", " local   GATE ", "LOCAL GATE"]
    ]
    messages.append(await finding_message(context, other_id, "Local gate"))
    await asyncio.gather(*(ScoutAgent().handle_message(message, context) for message in messages))
    rows = await capabilities(context)
    assert len(rows) == 2
    assert {row.objective_id for row in rows} == {objective_id, other_id}
    assert len({row.name for row in rows}) == 2
    assert (
        len(
            [
                event
                for event in await capability_events(context)
                if event.event_type == "capability.evidence_added"
            ]
        )
        == 4
    )


async def test_later_adoption_links_and_promotes_existing_discovery(context, objective_id):
    await ScoutAgent().handle_message(
        await finding_message(context, objective_id, "Local gate"), context
    )
    [original] = await capabilities(context)
    assert original.requirement_id is None
    adopted = await finding_message(context, objective_id, "LOCAL gate", confidence=0.8)
    await ScoutAgent().handle_message(adopted, context)
    [updated] = await capabilities(context)
    assert updated.id == original.id
    assert updated.lifecycle_status == "proposed"
    assert updated.requirement_id is not None
    [addition] = [
        event
        for event in await capability_events(context)
        if event.event_type == "capability.evidence_added"
        and event.payload["finding_id"] == adopted.payload["finding_id"]
    ]
    assert addition.payload["requirement_id"] == str(updated.requirement_id)


async def test_duplicate_names_in_one_finding_keep_the_adopted_requirement(context, objective_id):
    message = await finding_message(
        context,
        objective_id,
        "Local gate",
        confidence=0.8,
        alternatives=[{"name": "LOCAL GATE"}],
    )
    await ScoutAgent().handle_message(message, context)
    [row] = await capabilities(context)
    assert row.lifecycle_status == "proposed"
    assert row.requirement_id is not None
    assert (
        len(
            [
                event
                for event in await capability_events(context)
                if event.event_type == "capability.evidence_added"
            ]
        )
        == 1
    )


@pytest.mark.parametrize("verified", [False, True])
async def test_discovery_does_not_overwrite_reviewed_capability(context, objective_id, verified):
    await ScoutAgent().handle_message(
        await finding_message(
            context,
            objective_id,
            "Local gate",
            confidence=0.8,
            description="Reviewed description",
            dependencies=["Reviewed dependency"],
            cons=["Reviewed limitation"],
        ),
        context,
    )
    [original] = await capabilities(context)
    if verified:
        repository = CapabilityRepository(context.sessions)
        for status in (
            CapabilityStatus.APPROVED,
            CapabilityStatus.INSTALLED,
            CapabilityStatus.TESTED,
            CapabilityStatus.VERIFIED,
        ):
            await repository.transition(original.id, status, actor="LEDGER")
    [before] = await capabilities(context)
    await ScoutAgent().handle_message(
        await finding_message(
            context,
            objective_id,
            " LOCAL GATE ",
            confidence=0.8,
            description="Unreviewed replacement",
            dependencies=["Unreviewed dependency"],
            cons=["Unreviewed limitation"],
            resource_needs={"ram_mb": 999},
        ),
        context,
    )
    [after] = await capabilities(context)
    assert {
        column.name: getattr(after, column.name) for column in CapabilityRecord.__table__.columns
    } == {
        column.name: getattr(before, column.name) for column in CapabilityRecord.__table__.columns
    }
    additions = [
        event
        for event in await capability_events(context)
        if event.event_type == "capability.evidence_added"
    ]
    assert len(additions) == 2
    assert any(event.payload["description"] == "Unreviewed replacement" for event in additions)


async def test_existing_finding_scoped_name_is_reused(context, objective_id):
    repository = CapabilityRepository(context.sessions)
    old_id = await repository.register(
        name=f"Local gate [{uuid4()}]",
        actor="SCOUT",
        objective_id=objective_id,
        description="Old discovery",
    )
    await ScoutAgent().handle_message(
        await finding_message(context, objective_id, " local   GATE ", description="New evidence"),
        context,
    )
    [row] = await capabilities(context)
    assert row.id == old_id
    assert row.description == "New evidence"


async def test_long_names_have_distinct_stable_identities(context, objective_id):
    prefix = "x" * 180
    for name in [prefix + "first", prefix + "second", (prefix + "FIRST")]:
        await ScoutAgent().handle_message(
            await finding_message(context, objective_id, name), context
        )
    rows = await capabilities(context)
    assert len(rows) == 2
    assert all(len(row.name) <= 200 for row in rows)
