"""Relational knowledge graph of entities and typed relations."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    EventCategory,
    KnowledgeEntityRecord,
    KnowledgeRelationRecord,
    utcnow,
)
from aether.storage.repositories._events import add_event


class KnowledgeGraphRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert_entity(
        self,
        *,
        entity_type: str,
        name: str,
        actor: str,
        attributes: dict[str, Any] | None = None,
        person_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Return the entity id for ``(entity_type, name)``, merging any new attributes."""
        async with self._sessions() as session, session.begin():
            by_type = KnowledgeEntityRecord.entity_type == entity_type
            by_name = KnowledgeEntityRecord.name == name
            statement = select(KnowledgeEntityRecord).where(by_type, by_name)
            entity = await session.scalar(statement.with_for_update())
            created = entity is None
            if entity is None:
                entity = KnowledgeEntityRecord(entity_type=entity_type, name=name, attributes={})
                session.add(entity)
            if attributes:
                entity.attributes = {**entity.attributes, **attributes}
            if person_id is not None:
                entity.person_id = person_id
            entity.updated_at = utcnow()
            await session.flush()
            add_event(
                session,
                event_type="kg.entity_created" if created else "kg.entity_updated",
                aggregate_type="kg_entity",
                aggregate_id=entity.id,
                actor=actor,
                payload={"entity_type": entity_type, "name": name},
                category=EventCategory.KNOWLEDGE,
                correlation_id=correlation_id,
            )
            return entity.id

    async def add_relation(
        self,
        *,
        source_entity_id: UUID,
        target_entity_id: UUID,
        relation_type: str,
        actor: str,
        attributes: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Idempotently add a relation; an existing identical triple is returned unchanged."""
        async with self._sessions() as session, session.begin():
            conditions = (
                KnowledgeRelationRecord.source_entity_id == source_entity_id,
                KnowledgeRelationRecord.target_entity_id == target_entity_id,
                KnowledgeRelationRecord.relation_type == relation_type,
            )
            existing = await session.scalar(select(KnowledgeRelationRecord).where(*conditions))
            if existing is not None:
                return existing.id
            for entity_id in (source_entity_id, target_entity_id):
                if await session.get(KnowledgeEntityRecord, entity_id) is None:
                    raise LookupError(f"Knowledge graph entity {entity_id} does not exist")
            relation = KnowledgeRelationRecord(
                source_entity_id=source_entity_id,
                target_entity_id=target_entity_id,
                relation_type=relation_type,
                attributes=attributes or {},
            )
            session.add(relation)
            await session.flush()
            add_event(
                session,
                event_type="kg.relation_created",
                aggregate_type="kg_relation",
                aggregate_id=relation.id,
                actor=actor,
                payload={
                    "source_entity_id": source_entity_id,
                    "target_entity_id": target_entity_id,
                    "relation_type": relation_type,
                },
                category=EventCategory.KNOWLEDGE,
                correlation_id=correlation_id,
            )
            return relation.id

    async def relations_from(self, entity_id: UUID) -> list[KnowledgeRelationRecord]:
        condition = KnowledgeRelationRecord.source_entity_id == entity_id
        async with self._sessions() as session:
            records = await session.scalars(select(KnowledgeRelationRecord).where(condition))
            return list(records)
