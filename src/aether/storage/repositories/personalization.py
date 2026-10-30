"""People, their preferences, and interaction history used by MUSE."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    EventCategory,
    InteractionDirection,
    InteractionRecord,
    PersonRecord,
    PreferenceSource,
    UserPreferenceRecord,
    utcnow,
)
from aether.storage.repositories._events import add_event


class PersonRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        display_name: str,
        actor: str = "MUSE",
        relationship: str | None = None,
        access_level: str = "restricted",
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            person = PersonRecord(
                display_name=display_name,
                relationship=relationship,
                access_level=access_level,
            )
            session.add(person)
            await session.flush()
            add_event(
                session,
                event_type="person.created",
                aggregate_type="person",
                aggregate_id=person.id,
                actor=actor,
                payload={"relationship": relationship, "access_level": access_level},
                category=EventCategory.PERSONALIZATION,
                correlation_id=correlation_id,
            )
            return person.id

    async def soft_delete(
        self,
        person_id: UUID,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            person = await _lock_person(session, person_id)
            person.deleted_at = utcnow()
            add_event(
                session,
                event_type="person.deleted",
                aggregate_type="person",
                aggregate_id=person.id,
                actor=actor,
                payload={},
                category=EventCategory.PERSONALIZATION,
                correlation_id=correlation_id,
            )

    async def set_preference(
        self,
        person_id: UUID,
        key: str,
        value: Any,
        *,
        source: PreferenceSource = PreferenceSource.EXPLICIT,
        actor: str = "MUSE",
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Create or replace the single preference stored for ``(person_id, key)``."""
        source = PreferenceSource(source)
        async with self._sessions() as session, session.begin():
            await _lock_person(session, person_id)
            by_person = UserPreferenceRecord.person_id == person_id
            by_key = UserPreferenceRecord.key == key
            statement = select(UserPreferenceRecord).where(by_person, by_key)
            preference = await session.scalar(statement.with_for_update())
            previous = None if preference is None else preference.value
            if preference is None:
                preference = UserPreferenceRecord(person_id=person_id, key=key, value=value)
                session.add(preference)
            preference.value = value
            preference.source = source
            preference.updated_at = utcnow()
            await session.flush()
            add_event(
                session,
                event_type="preference.set",
                aggregate_type="user_preference",
                aggregate_id=preference.id,
                actor=actor,
                payload={
                    "person_id": person_id,
                    "key": key,
                    "previous": previous,
                    "value": value,
                    "source": source,
                },
                category=EventCategory.PERSONALIZATION,
                correlation_id=correlation_id,
            )
            return preference.id

    async def get_preferences(self, person_id: UUID) -> dict[str, Any]:
        statement = select(UserPreferenceRecord).where(UserPreferenceRecord.person_id == person_id)
        async with self._sessions() as session:
            return {record.key: record.value for record in await session.scalars(statement)}

    async def record_interaction(
        self,
        *,
        channel: str,
        direction: InteractionDirection,
        summary: str,
        actor: str = "MUSE",
        person_id: UUID | None = None,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        authorization_request_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        direction = InteractionDirection(direction)
        async with self._sessions() as session, session.begin():
            interaction = InteractionRecord(
                person_id=person_id,
                channel=channel,
                direction=direction,
                summary=summary,
                objective_id=objective_id,
                task_id=task_id,
                authorization_request_id=authorization_request_id,
            )
            session.add(interaction)
            await session.flush()
            add_event(
                session,
                event_type="interaction.recorded",
                aggregate_type="interaction",
                aggregate_id=interaction.id,
                actor=actor,
                payload={
                    "person_id": person_id,
                    "channel": channel,
                    "direction": direction,
                    "authorization_request_id": authorization_request_id,
                },
                category=EventCategory.PERSONALIZATION,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
            )
            return interaction.id


async def _lock_person(session: AsyncSession, person_id: UUID) -> PersonRecord:
    statement = select(PersonRecord).where(PersonRecord.id == person_id)
    person = await session.scalar(statement.with_for_update())
    if person is None or person.deleted_at is not None:
        raise LookupError(f"Person {person_id} does not exist")
    return person
