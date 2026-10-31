"""Local repository extensions until the shared personalization APIs expose these operations.

Interaction rows hold message content. Registered decision.recorded events hold typed checkpoints,
using the existing unique event idempotency key rather than introducing unmigrated tables.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.muse.schemas import (
    CommunicationPreferences,
    OutboxCheckpoint,
    ResponseCheckpoint,
    StoredOutbox,
)
from aether.communication.contracts import (
    CommunicationRequestPayload,
    UserDecision,
)
from aether.communication.messages import AgentMessage
from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    EventRecord,
    InteractionDirection,
    InteractionRecord,
    MessageRecord,
    ObjectiveRecord,
    PersonRecord,
    PreferenceSource,
    TaskRecord,
    UserPreferenceRecord,
)
from aether.storage.repositories._events import add_event, as_utc
from aether.storage.repositories.personalization import PersonRepository

_SOURCE = "agents.muse.repository"


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(operation: str, identifier: UUID) -> str:
    return f"muse:{operation}:{identifier}"


async def _serialize(session: AsyncSession, correlation_id: UUID, person_id: UUID | None) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": correlation_id.int % (2**63 - 1)},
        )
    elif session.get_bind().dialect.name == "sqlite":
        # Obtain SQLite's write lock before reading dedupe state, even for anonymous audiences.
        await session.execute(
            update(PersonRecord)
            .where(PersonRecord.id == person_id)
            .values(deleted_at=PersonRecord.deleted_at)
        )


async def _person(session: AsyncSession, person_id: UUID | None) -> None:
    if person_id is None:
        return
    access = await session.scalar(
        select(PersonRecord.access_level)
        .where(PersonRecord.id == person_id, PersonRecord.deleted_at.is_(None))
        .with_for_update()
    )
    if access is None:
        raise LookupError("Audience does not exist or was deleted")
    if access in {"none", "denied", "deleted"}:
        raise PermissionError("Audience access is denied")


def _decision(
    session: AsyncSession,
    operation: str,
    identifier: UUID,
    *,
    correlation_id: UUID,
    evidence: list[str] | None = None,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
) -> None:
    add_event(
        session,
        event_type="decision.recorded",
        aggregate_type="muse_workflow",
        aggregate_id=identifier,
        actor="MUSE",
        payload={
            "decision": f"muse.{operation}",
            "rationale": "Explicit communication workflow checkpoint",
            "evidence": evidence or [],
        },
        category="decision",
        source=_SOURCE,
        correlation_id=correlation_id,
        objective_id=objective_id,
        task_id=task_id,
        idempotency_key=_key(operation, identifier),
    )


async def _event(session: AsyncSession, operation: str, identifier: UUID) -> EventRecord | None:
    return await session.scalar(
        select(EventRecord).where(
            EventRecord.source == _SOURCE,
            EventRecord.idempotency_key == _key(operation, identifier),
        )
    )


def _interaction_event(session: AsyncSession, interaction: InteractionRecord, correlation_id: UUID):
    add_event(
        session,
        event_type="interaction.recorded",
        aggregate_type="interaction",
        aggregate_id=interaction.id,
        actor="MUSE",
        payload={
            "person_id": interaction.person_id,
            "channel": interaction.channel,
            "direction": interaction.direction,
            "authorization_request_id": interaction.authorization_request_id,
        },
        category="personalization",
        source=_SOURCE,
        correlation_id=correlation_id,
        objective_id=interaction.objective_id,
        task_id=interaction.task_id,
    )


async def _stored_outbox(session: AsyncSession, event: EventRecord) -> StoredOutbox | None:
    if not event.payload["evidence"]:
        return None
    checkpoint = OutboxCheckpoint.model_validate_json(event.payload["evidence"][0])
    await _person(session, checkpoint.audience)
    interaction = await session.get(InteractionRecord, checkpoint.interaction_id)
    if interaction is None:
        raise LookupError("MUSE outbox interaction is missing")
    delivered = await _event(session, "delivered", checkpoint.interaction_id)
    delivered_at = (
        datetime.fromisoformat(delivered.payload["evidence"][0]) if delivered is not None else None
    )
    return StoredOutbox(checkpoint=checkpoint, text=interaction.summary, delivered_at=delivered_at)


class MuseRepository(PersonRepository):
    async def record_control(self, message: AgentMessage) -> None:
        async with self._sessions() as session, session.begin():
            await _serialize(session, message.correlation_id, None)
            if await _event(session, "control", message.message_id) is None:
                _decision(
                    session,
                    "control",
                    message.message_id,
                    correlation_id=message.correlation_id,
                    evidence=[message.message_type.value],
                    objective_id=message.objective_id,
                    task_id=message.task_id,
                )

    async def response_correlation(
        self,
        person_id: UUID | None,
        authorization_id: UUID | None,
        objective_id: UUID | None,
        task_id: UUID | None,
        response_id: UUID | None = None,
    ) -> UUID | None:
        async with self._sessions() as session:
            await _person(session, person_id)
            if response_id is not None:
                existing = await _event(session, "response", response_id)
                if existing is not None:
                    return ResponseCheckpoint.model_validate_json(
                        existing.payload["evidence"][0]
                    ).correlation_id
            statement = (
                select(EventRecord.correlation_id)
                .join(InteractionRecord, InteractionRecord.id == EventRecord.aggregate_id)
                .where(
                    EventRecord.source == _SOURCE,
                    EventRecord.event_type == "interaction.recorded",
                    InteractionRecord.person_id == person_id,
                    InteractionRecord.direction == InteractionDirection.OUTBOUND,
                )
            )
            if authorization_id is not None:
                statement = statement.where(
                    InteractionRecord.authorization_request_id == authorization_id
                )
            elif task_id is not None:
                statement = statement.where(InteractionRecord.task_id == task_id)
            elif objective_id is not None:
                statement = statement.where(InteractionRecord.objective_id == objective_id)
            else:
                return None
            return await session.scalar(
                statement.order_by(InteractionRecord.occurred_at.desc()).limit(1)
            )

    async def get_preferences(self, person_id: UUID) -> dict:
        async with self._sessions() as session:
            await _person(session, person_id)
            rows = await session.execute(
                select(UserPreferenceRecord.key, UserPreferenceRecord.value)
                .join(PersonRecord, PersonRecord.id == UserPreferenceRecord.person_id)
                .where(
                    PersonRecord.deleted_at.is_(None),
                    UserPreferenceRecord.person_id == person_id,
                    UserPreferenceRecord.key.in_(CommunicationPreferences.model_fields),
                )
            )
            return dict(rows.all())

    async def lookup_outbox(self, request_id: UUID) -> tuple[bool, StoredOutbox | None]:
        async with self._sessions() as session:
            event = await _event(session, "outbox", request_id)
            return (False, None) if event is None else (True, await _stored_outbox(session, event))

    async def prepare_outbox(
        self,
        message: AgentMessage,
        request: CommunicationRequestPayload,
        *,
        rendered: str,
        canonical_text: str,
        channel: str,
        data_dir: Path,
        now: datetime,
        dedupe_window: timedelta,
        suppress: bool = False,
    ) -> StoredOutbox | None:
        try:
            async with self._sessions() as session, session.begin():
                await _serialize(session, message.correlation_id, request.audience)
                await _person(session, request.audience)
                existing = await _event(session, "outbox", message.message_id)
                if existing is not None:
                    return await _stored_outbox(session, existing)
                if suppress:
                    _decision(
                        session,
                        "outbox",
                        message.message_id,
                        correlation_id=message.correlation_id,
                        objective_id=request.objective_id,
                        task_id=request.task_id,
                    )
                    return None
                await self._validate_links(
                    session, request.objective_id, request.task_id, request.authorization_request_id
                )
                candidates = await session.scalars(
                    select(EventRecord).where(
                        EventRecord.source == _SOURCE,
                        EventRecord.correlation_id == message.correlation_id,
                        EventRecord.payload["decision"].as_string() == "muse.outbox",
                    )
                )
                for candidate in candidates:
                    if not candidate.payload["evidence"]:
                        continue
                    saved = OutboxCheckpoint.model_validate_json(candidate.payload["evidence"][0])
                    age = now - saved.prepared_at
                    if (
                        timedelta(0) <= age <= dedupe_window
                        and saved.audience == request.audience
                        and saved.channel == channel
                        and saved.content_hash == text_hash(canonical_text)
                        and saved.objective_id == request.objective_id
                        and saved.task_id == request.task_id
                        and saved.requirement_id == message.requirement_id
                    ):
                        _decision(
                            session,
                            "outbox",
                            message.message_id,
                            correlation_id=message.correlation_id,
                            evidence=[saved.model_dump_json()],
                            objective_id=request.objective_id,
                            task_id=request.task_id,
                        )
                        return await _stored_outbox(session, candidate)
                interaction_id = uuid5(message.message_id, "muse.outbox")
                checkpoint = OutboxCheckpoint(
                    request_message_id=message.message_id,
                    interaction_id=interaction_id,
                    audience=request.audience,
                    channel=channel,
                    path=data_dir
                    / "outbox"
                    / f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{interaction_id}.md",
                    content_hash=text_hash(canonical_text),
                    rendered_text_hash=text_hash(rendered),
                    correlation_id=message.correlation_id,
                    objective_id=request.objective_id,
                    task_id=request.task_id,
                    requirement_id=message.requirement_id,
                    prepared_at=now,
                )
                interaction = InteractionRecord(
                    id=interaction_id,
                    person_id=request.audience,
                    channel=channel,
                    direction=InteractionDirection.OUTBOUND,
                    summary=rendered,
                    objective_id=request.objective_id,
                    task_id=request.task_id,
                    authorization_request_id=(
                        request.authorization_request_id if request.authorization_required else None
                    ),
                    occurred_at=now,
                )
                session.add(interaction)
                await session.flush()
                _interaction_event(session, interaction, message.correlation_id)
                _decision(
                    session,
                    "outbox",
                    message.message_id,
                    correlation_id=message.correlation_id,
                    evidence=[checkpoint.model_dump_json()],
                    objective_id=request.objective_id,
                    task_id=request.task_id,
                )
                return StoredOutbox(checkpoint=checkpoint, text=rendered)
        except IntegrityError:
            # Only an identical idempotency key winning concurrently is recoverable.
            found, stored = await self.lookup_outbox(message.message_id)
            if not found:
                raise
            return stored

    async def mark_delivered(self, stored: StoredOutbox, now: datetime) -> datetime:
        saved = stored.checkpoint
        async with self._sessions() as session, session.begin():
            await _serialize(session, saved.correlation_id, saved.audience)
            await _person(session, saved.audience)
            event = await _event(session, "delivered", saved.interaction_id)
            if event is not None:
                return datetime.fromisoformat(event.payload["evidence"][0])
            _decision(
                session,
                "delivered",
                saved.interaction_id,
                correlation_id=saved.correlation_id,
                evidence=[now.isoformat()],
                objective_id=saved.objective_id,
                task_id=saved.task_id,
            )
            return now

    async def outboxes(self, task_id: UUID | None = None) -> list[StoredOutbox]:
        async with self._sessions() as session:
            statement = select(EventRecord).where(
                EventRecord.source == _SOURCE,
                EventRecord.payload["decision"].as_string() == "muse.outbox",
            )
            if task_id is not None:
                statement = statement.where(EventRecord.task_id == task_id)
            events = list(await session.scalars(statement.order_by(EventRecord.created_at)))
            stored = []
            seen: set[UUID] = set()
            for event in events:
                if not event.payload["evidence"]:
                    continue
                checkpoint = OutboxCheckpoint.model_validate_json(event.payload["evidence"][0])
                if checkpoint.interaction_id in seen:
                    continue
                if checkpoint.audience is not None:
                    active = await session.scalar(
                        select(PersonRecord.id).where(
                            PersonRecord.id == checkpoint.audience,
                            PersonRecord.deleted_at.is_(None),
                            PersonRecord.access_level.not_in(["none", "denied", "deleted"]),
                        )
                    )
                    if active is None:
                        continue
                seen.add(checkpoint.interaction_id)
                if task_id is None and await _event(
                    session, "delivered", checkpoint.interaction_id
                ):
                    published = [
                        await session.get(
                            MessageRecord, uuid5(checkpoint.interaction_id, f"delivered:{target}")
                        )
                        is not None
                        for target in ("ORCHESTR", "LEDGER")
                    ]
                    if all(published):
                        continue
                item = await _stored_outbox(session, event)
                if item is not None:
                    stored.append(item)
            return stored

    @staticmethod
    async def _validate_links(
        session: AsyncSession,
        objective_id: UUID | None,
        task_id: UUID | None,
        authorization_id: UUID | None,
    ) -> tuple[UUID | None, UUID | None, AuthorizationRequestRecord | None]:
        authorization = None
        if authorization_id is not None:
            authorization = await session.scalar(
                select(AuthorizationRequestRecord)
                .where(AuthorizationRequestRecord.id == authorization_id)
                .with_for_update()
            )
            if authorization is None:
                raise LookupError("Authorization request does not exist")
            if (
                objective_id is not None
                and authorization.objective_id is not None
                and objective_id != authorization.objective_id
            ) or (
                task_id is not None
                and authorization.task_id is not None
                and task_id != authorization.task_id
            ):
                raise ValueError("Response or handoff links contradict the authorization request")
            objective_id = objective_id or authorization.objective_id
            task_id = task_id or authorization.task_id
        if task_id is not None:
            task = await session.get(TaskRecord, task_id)
            if task is None:
                raise LookupError("Task does not exist")
            if objective_id is not None and objective_id != task.objective_id:
                raise ValueError("Objective does not own the referenced task")
            objective_id = objective_id or task.objective_id
        if objective_id is not None and await session.get(ObjectiveRecord, objective_id) is None:
            raise LookupError("Objective does not exist")
        return objective_id, task_id, authorization

    async def record_response(
        self, checkpoint: ResponseCheckpoint, now: datetime
    ) -> ResponseCheckpoint:
        async with self._sessions() as session, session.begin():
            await _serialize(session, checkpoint.correlation_id, checkpoint.person_id)
            await _person(session, checkpoint.person_id)
            existing = await _event(session, "response", checkpoint.response_id)
            if existing is not None:
                saved = await self._stored_response(session, existing)
                left = saved.model_dump(exclude={"response": {"responded_at"}})
                right = checkpoint.model_dump(exclude={"response": {"responded_at"}})
                # Authorization-only input is expanded to its authoritative objective/task links.
                for name in ("objective_id", "task_id"):
                    if right["response"][name] is None:
                        right["response"][name] = left["response"][name]
                if left != right:
                    raise ValueError("Response id was reused for different input")
                return saved
            response = checkpoint.response
            objective, task, authorization = await self._validate_links(
                session, response.objective_id, response.task_id, response.authorization_request_id
            )
            if authorization is not None:
                addressed = await session.scalar(
                    select(InteractionRecord.id).where(
                        InteractionRecord.authorization_request_id == authorization.id,
                        InteractionRecord.direction == InteractionDirection.OUTBOUND,
                        InteractionRecord.person_id == checkpoint.person_id,
                    )
                )
                if addressed is None or await _event(session, "delivered", addressed) is None:
                    raise PermissionError(
                        "Authorization request was not addressed to this audience"
                    )
                expires = as_utc(authorization.expires_at)
                if response.decision is not UserDecision.NONE and (
                    authorization.status != AuthorizationStatus.PENDING
                    or (expires is not None and now >= expires)
                ):
                    raise ValueError("Authorization request is not pending or has expired")
            response = response.model_copy(update={"objective_id": objective, "task_id": task})
            saved = checkpoint.model_copy(update={"response": response})
            interaction = InteractionRecord(
                id=saved.interaction_id,
                person_id=saved.person_id,
                channel=saved.channel,
                direction=InteractionDirection.INBOUND,
                summary=response.text,
                objective_id=objective,
                task_id=task,
                authorization_request_id=response.authorization_request_id,
                occurred_at=now,
            )
            session.add(interaction)
            await session.flush()
            _interaction_event(session, interaction, saved.correlation_id)
            _decision(
                session,
                "response",
                saved.response_id,
                correlation_id=saved.correlation_id,
                evidence=[
                    saved.model_copy(
                        update={"response": response.model_copy(update={"text": ""})}
                    ).model_dump_json()
                ],
                objective_id=objective,
                task_id=task,
            )
            return saved

    @staticmethod
    async def _stored_response(session: AsyncSession, event: EventRecord) -> ResponseCheckpoint:
        saved = ResponseCheckpoint.model_validate_json(event.payload["evidence"][0])
        await _person(session, saved.person_id)
        interaction = await session.get(InteractionRecord, saved.interaction_id)
        if interaction is None:
            raise LookupError("MUSE response interaction is missing")
        return saved.model_copy(
            update={"response": saved.response.model_copy(update={"text": interaction.summary})}
        )

    async def pending_responses(self) -> list[ResponseCheckpoint]:
        async with self._sessions() as session:
            events = list(
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.source == _SOURCE,
                        EventRecord.payload["decision"].as_string() == "muse.response",
                    )
                    .order_by(EventRecord.created_at)
                )
            )
            pending = []
            for event in events:
                if await _event(session, "response_forwarded", event.aggregate_id) is not None:
                    continue
                saved = ResponseCheckpoint.model_validate_json(event.payload["evidence"][0])
                if saved.person_id is not None:
                    active = await session.scalar(
                        select(PersonRecord.id).where(
                            PersonRecord.id == saved.person_id, PersonRecord.deleted_at.is_(None)
                        )
                    )
                    if active is None:
                        continue
                pending.append(await self._stored_response(session, event))
            return pending

    async def mark_response_forwarded(self, checkpoint: ResponseCheckpoint) -> None:
        async with self._sessions() as session, session.begin():
            await _serialize(session, checkpoint.correlation_id, checkpoint.person_id)
            await _person(session, checkpoint.person_id)
            if await _event(session, "response_forwarded", checkpoint.response_id) is None:
                _decision(
                    session,
                    "response_forwarded",
                    checkpoint.response_id,
                    correlation_id=checkpoint.correlation_id,
                    objective_id=checkpoint.response.objective_id,
                    task_id=checkpoint.response.task_id,
                )

    async def record_feedback(
        self,
        person_id: UUID,
        feedback_id: UUID,
        changes: dict[str, str],
        *,
        text: str,
        correlation_id: UUID,
        now: datetime,
    ) -> None:
        async with self._sessions() as session, session.begin():
            await _serialize(session, correlation_id, person_id)
            await _person(session, person_id)
            existing = await _event(session, "feedback", feedback_id)
            evidence = json.dumps({"person_id": str(person_id), "changes": changes}, sort_keys=True)
            if existing is not None:
                if existing.payload["evidence"] != [evidence]:
                    raise ValueError("Feedback id was reused for different input")
                return
            for key, value in changes.items():
                preference = await session.scalar(
                    select(UserPreferenceRecord).where(
                        UserPreferenceRecord.person_id == person_id, UserPreferenceRecord.key == key
                    )
                )
                previous = preference.value if preference else None
                if preference is None:
                    preference = UserPreferenceRecord(person_id=person_id, key=key, value=value)
                    session.add(preference)
                preference.value = value
                preference.source = PreferenceSource.FEEDBACK
                preference.updated_at = now
                await session.flush()
                add_event(
                    session,
                    event_type="preference.set",
                    aggregate_type="user_preference",
                    aggregate_id=preference.id,
                    actor="MUSE",
                    payload={
                        "person_id": person_id,
                        "name": key,
                        "previous": previous,
                        "value": value,
                        "source": PreferenceSource.FEEDBACK,
                    },
                    category="personalization",
                    correlation_id=correlation_id,
                    source=_SOURCE,
                )
            interaction = InteractionRecord(
                id=uuid5(feedback_id, "muse.feedback"),
                person_id=person_id,
                channel="console",
                direction=InteractionDirection.INBOUND,
                summary=text,
                occurred_at=now,
            )
            session.add(interaction)
            await session.flush()
            _interaction_event(session, interaction, correlation_id)
            _decision(
                session, "feedback", feedback_id, correlation_id=correlation_id, evidence=[evidence]
            )
