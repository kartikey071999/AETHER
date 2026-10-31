from uuid import uuid4

import pytest
from sqlalchemy import func, select

from aether.storage.models import (
    EventRecord,
    InteractionDirection,
    InteractionRecord,
    KnowledgeEntityRecord,
    KnowledgeRelationRecord,
    PersonRecord,
    UserPreferenceRecord,
)
from aether.storage.repositories import KnowledgeGraphRepository, PersonRepository


async def count(database, model) -> int:
    async with database.sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_deleted_or_unknown_person_has_no_preferences(database):
    people = PersonRepository(database.sessions)
    person_id = await people.create(display_name="Owner", access_level="full")
    await people.set_preference(person_id, "tone", "warm")
    assert await people.get_preferences(person_id) == {"tone": "warm"}

    await people.soft_delete(person_id, actor="MUSE")

    assert await people.get_preferences(person_id) == {}
    assert await people.get_preferences(uuid4()) == {}


async def _seeded(database):
    people = PersonRepository(database.sessions)
    graph = KnowledgeGraphRepository(database.sessions)
    person_id = await people.create(display_name="Owner", relationship="owner", access_level="full")
    await people.set_preference(person_id, "tone", "warm")
    await people.set_preference(person_id, "length", "short")
    owned = await graph.upsert_entity(
        entity_type="project", name="Owned", actor="MUSE", person_id=person_id
    )
    shared = await graph.upsert_entity(entity_type="project", name="Shared", actor="MUSE")
    await graph.add_relation(
        source_entity_id=shared, target_entity_id=owned, relation_type="references", actor="MUSE"
    )
    interaction = await people.record_interaction(
        channel="file",
        direction=InteractionDirection.OUTBOUND,
        summary="kept",
        person_id=person_id,
    )
    return people, person_id, owned, shared, interaction


async def test_delete_person_data_anonymizes_purges_and_audits_once(database):
    people, person_id, owned, shared, interaction = await _seeded(database)

    assert await people.delete_person_data(person_id, "MUSE", correlation_id=uuid4()) is True

    async with database.sessions() as session:
        person = await session.get(PersonRecord, person_id)
        assert person.deleted_at is not None
        assert (person.display_name, person.relationship, person.access_level) == (
            "Deleted person",
            None,
            "deleted",
        )
        assert await session.get(KnowledgeEntityRecord, owned) is None
        assert await session.get(KnowledgeEntityRecord, shared) is not None
        assert await session.get(InteractionRecord, interaction) is not None
        events = list(
            await session.scalars(
                select(EventRecord).where(
                    EventRecord.aggregate_id == person_id,
                    EventRecord.event_type == "person.deleted",
                )
            )
        )
    assert len(events) == 1
    assert events[0].actor == "MUSE"
    assert await count(database, UserPreferenceRecord) == 0
    assert await count(database, KnowledgeRelationRecord) == 0
    assert await people.get_preferences(person_id) == {}
    # Earlier audit events stay immutable and present.
    async with database.sessions() as session:
        created = await session.scalar(
            select(func.count())
            .select_from(EventRecord)
            .where(
                EventRecord.aggregate_id == person_id, EventRecord.event_type == "person.created"
            )
        )
    assert created == 1


async def test_delete_person_data_is_idempotent_and_rejects_unknown(database):
    people, person_id, *_ = await _seeded(database)

    assert await people.delete_person_data(person_id, "MUSE") is True
    assert await people.delete_person_data(person_id, "MUSE") is False
    with pytest.raises(LookupError):
        await people.delete_person_data(uuid4(), "MUSE")

    async with database.sessions() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(EventRecord)
            .where(
                EventRecord.aggregate_id == person_id, EventRecord.event_type == "person.deleted"
            )
        )
    assert total == 1


async def test_delete_person_data_is_atomic(database, monkeypatch):
    people, person_id, owned, _shared, _interaction = await _seeded(database)

    def explode(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr("aether.storage.repositories.personalization.add_event", explode)
    with pytest.raises(RuntimeError):
        await people.delete_person_data(person_id, "MUSE")
    monkeypatch.undo()

    async with database.sessions() as session:
        person = await session.get(PersonRecord, person_id)
        assert person.deleted_at is None
        assert person.display_name == "Owner"
        assert person.access_level == "full"
        assert await session.get(KnowledgeEntityRecord, owned) is not None
    assert await count(database, UserPreferenceRecord) == 2
    assert await count(database, KnowledgeRelationRecord) == 1
    assert await people.get_preferences(person_id) == {"tone": "warm", "length": "short"}


async def _scoped_setup(database, access_level="full", name="Atlas"):
    people = PersonRepository(database.sessions)
    graph = KnowledgeGraphRepository(database.sessions)
    person_id = await people.create(display_name="Owner", access_level=access_level)
    objective_id, task_id = uuid4(), uuid4()
    entity = await graph.upsert_entity(
        entity_type="project",
        name=name,
        actor="MUSE",
        person_id=person_id,
        attributes={"objective_id": str(objective_id), "secret": "never returned"},
    )
    return people, graph, person_id, entity, objective_id, task_id


async def test_scoped_kg_reads_return_only_allowlisted_matching_names(database):
    _, graph, person_id, entity, objective_id, task_id = await _scoped_setup(database)

    assert await graph.scoped_entity_names(
        person_id, entity_ids=[entity], names=["Atlas"], objective_id=objective_id
    ) == ["Atlas"]
    # Names are optional; the entity allowlist is mandatory.
    assert await graph.scoped_entity_names(
        person_id, entity_ids=[entity], objective_id=objective_id
    ) == ["Atlas"]
    assert (
        await graph.scoped_entity_names(person_id, entity_ids=[], objective_id=objective_id) == []
    )
    assert (
        await graph.scoped_entity_names(
            person_id, entity_ids=[uuid4()], names=["Atlas"], objective_id=objective_id
        )
        == []
    )
    assert (
        await graph.scoped_entity_names(
            person_id, entity_ids=[entity], names=["Other"], objective_id=objective_id
        )
        == []
    )
    # No objective/task context or a non-matching one yields nothing.
    assert await graph.scoped_entity_names(person_id, entity_ids=[entity]) == []
    assert await graph.scoped_entity_names(person_id, entity_ids=[entity], task_id=task_id) == []
    assert (
        await graph.scoped_entity_names(person_id, entity_ids=[entity], objective_id=uuid4()) == []
    )
    # Another person's entity is never exposed.
    assert (
        await graph.scoped_entity_names(uuid4(), entity_ids=[entity], objective_id=objective_id)
        == []
    )


async def test_scoped_kg_reads_require_active_full_access_owner(database):
    people, graph, person_id, entity, objective_id, _ = await _scoped_setup(database)
    _, _, guest_id, guest_entity, guest_objective, _ = await _scoped_setup(
        database, access_level="restricted", name="Guest project"
    )
    assert (
        await graph.scoped_entity_names(
            guest_id, entity_ids=[guest_entity], objective_id=guest_objective
        )
        == []
    )

    await people.delete_person_data(person_id, "MUSE")
    assert (
        await graph.scoped_entity_names(person_id, entity_ids=[entity], objective_id=objective_id)
        == []
    )
