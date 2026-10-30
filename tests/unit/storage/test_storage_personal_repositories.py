from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aether.storage.models import (
    InteractionDirection,
    KnowledgeEntityRecord,
    KnowledgeRelationRecord,
    PreferenceSource,
    UserPreferenceRecord,
)
from aether.storage.repositories import (
    KnowledgeGraphRepository,
    PersonRepository,
    ProviderUsageRepository,
)


async def test_preference_upsert_keeps_one_row_per_key(database, events_for):
    people = PersonRepository(database.sessions)
    person_id = await people.create(display_name="Owner", relationship="owner", access_level="full")

    first = await people.set_preference(person_id, "tone", "concise")
    second = await people.set_preference(
        person_id,
        "tone",
        {"style": "direct"},
        source=PreferenceSource.FEEDBACK,
    )

    assert first == second
    assert await people.get_preferences(person_id) == {"tone": {"style": "direct"}}
    async with database.sessions() as session:
        statement = select(func.count()).select_from(UserPreferenceRecord)
        assert await session.scalar(statement) == 1
    events = await events_for(first)
    assert [event.event_type for event in events] == ["preference.set", "preference.set"]
    assert sorted(event.payload["source"] for event in events) == ["explicit", "feedback"]
    assert {event.category for event in events} == {"personalization"}


async def test_duplicate_preference_rows_violate_unique_constraint(database):
    person_id = await PersonRepository(database.sessions).create(display_name="Owner")

    async with database.sessions() as session:
        session.add(UserPreferenceRecord(person_id=person_id, key="tone", value="concise"))
        session.add(UserPreferenceRecord(person_id=person_id, key="tone", value="verbose"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_deleted_person_cannot_receive_preferences(database, events_for):
    people = PersonRepository(database.sessions)
    person_id = await people.create(display_name="Former contact")
    await people.soft_delete(person_id, actor="MUSE")

    with pytest.raises(LookupError):
        await people.set_preference(person_id, "tone", "formal")

    assert sorted(event.event_type for event in await events_for(person_id)) == [
        "person.created",
        "person.deleted",
    ]


async def test_interactions_are_recorded_with_events(database, events_for, objective_id):
    people = PersonRepository(database.sessions)
    person_id = await people.create(display_name="Owner")

    interaction_id = await people.record_interaction(
        channel="cli",
        direction=InteractionDirection.OUTBOUND,
        summary="Asked for installation approval",
        person_id=person_id,
        objective_id=objective_id,
    )

    events = await events_for(interaction_id)
    assert [event.event_type for event in events] == ["interaction.recorded"]
    assert events[0].objective_id == objective_id
    assert events[0].payload["direction"] == "outbound"


async def test_knowledge_graph_entities_merge_and_relations_are_idempotent(database, events_for):
    graph = KnowledgeGraphRepository(database.sessions)
    python = await graph.upsert_entity(
        entity_type="tool",
        name="python",
        actor="SCOUT",
        attributes={"version": "3.12"},
    )
    same = await graph.upsert_entity(
        entity_type="tool",
        name="python",
        actor="SCOUT",
        attributes={"license": "PSF"},
    )
    ocr = await graph.upsert_entity(entity_type="capability", name="ocr", actor="SCOUT")

    relation = await graph.add_relation(
        source_entity_id=ocr,
        target_entity_id=python,
        relation_type="requires",
        actor="SCOUT",
    )
    repeated = await graph.add_relation(
        source_entity_id=ocr,
        target_entity_id=python,
        relation_type="requires",
        actor="SCOUT",
    )
    with pytest.raises(LookupError):
        await graph.add_relation(
            source_entity_id=ocr,
            target_entity_id=uuid4(),
            relation_type="requires",
            actor="SCOUT",
        )

    assert same == python
    assert repeated == relation
    assert [record.id for record in await graph.relations_from(ocr)] == [relation]
    async with database.sessions() as session:
        entity = await session.get(KnowledgeEntityRecord, python)
    assert entity.attributes == {"version": "3.12", "license": "PSF"}
    assert [event.event_type for event in await events_for(relation)] == ["kg.relation_created"]
    assert sorted(event.event_type for event in await events_for(python)) == [
        "kg.entity_created",
        "kg.entity_updated",
    ]


async def test_duplicate_knowledge_relation_rows_violate_unique_constraint(database):
    graph = KnowledgeGraphRepository(database.sessions)
    source = await graph.upsert_entity(entity_type="capability", name="ocr", actor="SCOUT")
    target = await graph.upsert_entity(entity_type="tool", name="python", actor="SCOUT")
    await graph.add_relation(
        source_entity_id=source,
        target_entity_id=target,
        relation_type="requires",
        actor="SCOUT",
    )

    async with database.sessions() as session:
        session.add(
            KnowledgeRelationRecord(
                source_entity_id=source,
                target_entity_id=target,
                relation_type="requires",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_provider_usage_totals_and_events(database, events_for):
    usage = ProviderUsageRepository(database.sessions)
    first = await usage.record(
        provider="openai",
        model="gpt-test",
        agent="SCOUT",
        input_tokens=10,
        output_tokens=5,
        cost_usd="0.25",
    )
    await usage.record(provider="openai", model="gpt-test", cost_usd=Decimal("0.5"))
    await usage.record(provider="local", model="llama-test", cost_usd=0)

    with pytest.raises(ValueError):
        await usage.record(provider="openai", model="gpt-test", input_tokens=-1)
    with pytest.raises(ValueError):
        await usage.record(provider="openai", model="gpt-test", cost_usd="-0.01")

    assert await usage.total_cost() == Decimal("0.75")
    assert await usage.total_cost(provider="openai") == Decimal("0.75")
    assert await usage.total_cost(provider="local") == Decimal("0")
    assert await usage.total_cost(provider="missing") == Decimal("0")
    events = await events_for(first)
    assert [event.event_type for event in events] == ["provider.usage_recorded"]
    assert events[0].payload["cost_usd"] == "0.25"
    assert events[0].actor == "SCOUT"
