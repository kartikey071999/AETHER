"""Durable message bus: the database is the source of truth, asyncio only wakes consumers."""

import asyncio
import time
from collections.abc import Collection
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId, AgentMessage, MessageStatus, MessageType
from aether.runtime._support import Clock, add_event, as_utc, utc_now
from aether.storage.models import MessageRecord

_ALLOWED_MESSAGE_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.PENDING: frozenset({MessageStatus.DELIVERED}),
    MessageStatus.DELIVERED: frozenset({MessageStatus.PROCESSING}),
    MessageStatus.PROCESSING: frozenset({MessageStatus.PROCESSED, MessageStatus.FAILED}),
    MessageStatus.PROCESSED: frozenset(),
    MessageStatus.FAILED: frozenset(),
}

_IN_FLIGHT = (MessageStatus.DELIVERED.value, MessageStatus.PROCESSING.value)
_SOURCE = "communication.bus"
DEFAULT_CLAIM_TTL = timedelta(seconds=60)


class InvalidMessageTransition(ValueError):
    pass


class MessageBus:
    """Durable bus. A claimed message belongs to its consumer until the claim expires.

    ``claim`` stamps the consumer and an expiry; a live consumer keeps its claims alive with
    :meth:`renew_claims`. Only claims that are expired (or that predate claims and have no
    expiry) are requeued by :meth:`requeue_in_flight`, so a second engine starting against the
    same database never takes messages a running engine is still working on.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock = utc_now,
        poll_interval: float = 1.0,
        claim_ttl: timedelta = DEFAULT_CLAIM_TTL,
    ) -> None:
        if claim_ttl <= timedelta(0):
            raise ValueError("claim_ttl must be positive")
        self._sessions = sessions
        self._clock = clock
        self._poll_interval = poll_interval
        self._claim_ttl = claim_ttl
        self._wakeups: dict[str, asyncio.Event] = {}

    @property
    def claim_ttl(self) -> timedelta:
        return self._claim_ttl

    async def publish(self, message: AgentMessage) -> bool:
        """Persist a message; returns False when this message_id was already published."""
        created = False
        try:
            async with self._sessions() as session, session.begin():
                if await session.get(MessageRecord, message.message_id) is None:
                    session.add(self._to_record(message))
                    add_event(
                        session,
                        event_type="message.published",
                        aggregate_type="message",
                        aggregate_id=message.message_id,
                        actor=message.source_agent.value,
                        payload={
                            "message_id": message.message_id,
                            "source_agent": message.source_agent,
                            "target_agent": message.target_agent,
                            "message_type": message.message_type,
                        },
                        idempotency_key=f"message.published:{message.message_id}",
                        created_at=self._clock(),
                        source=_SOURCE,
                        objective_id=message.objective_id,
                        task_id=message.task_id,
                        correlation_id=message.correlation_id,
                    )
                    created = True
        except IntegrityError:
            # A concurrent publisher of the same id may have won; anything else is a real error.
            created = False
            async with self._sessions() as session:
                if await session.get(MessageRecord, message.message_id) is None:
                    raise
        self._wake(message.target_agent.value)
        return created

    async def claim(
        self, agent_id: AgentId, limit: int = 10, *, consumer: str
    ) -> list[AgentMessage]:
        """Atomically move up to `limit` pending messages for the agent to delivered."""
        if limit < 1:
            return []
        claimed: list[AgentMessage] = []
        now = self._clock()
        expires_at = now + self._claim_ttl
        async with self._sessions() as session, session.begin():
            statement = (
                select(MessageRecord)
                .where(
                    MessageRecord.target_agent == agent_id.value,
                    MessageRecord.status == MessageStatus.PENDING.value,
                )
                .order_by(MessageRecord.priority.desc(), MessageRecord.timestamp, MessageRecord.id)
                .limit(limit)
            )
            if session.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            for row in (await session.scalars(statement)).all():
                # Compare-and-set keeps dialects without row locks (SQLite) exclusive.
                result = await session.execute(
                    update(MessageRecord)
                    .where(
                        MessageRecord.id == row.id,
                        MessageRecord.status == MessageStatus.PENDING.value,
                    )
                    .values(
                        status=MessageStatus.DELIVERED.value,
                        claimed_by=consumer,
                        claimed_at=now,
                        claim_expires_at=expires_at,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    continue
                claimed.append(self._to_message(row, MessageStatus.DELIVERED))
                self._stage_status_event(
                    session,
                    row,
                    "message.delivered",
                    {"consumer": consumer},
                )
        return claimed

    async def mark_processing(self, message_id: UUID, *, consumer: str | None = None) -> None:
        """Start processing; with ``consumer`` the claim must still be that consumer's."""
        await self._advance(
            message_id,
            MessageStatus.PROCESSING,
            "message.processing",
            {"consumer": consumer},
            consumer=consumer,
            renew=True,
        )

    async def mark_processed(self, message_id: UUID, *, consumer: str | None = None) -> None:
        await self._advance(
            message_id, MessageStatus.PROCESSED, "message.processed", {}, consumer=consumer
        )

    async def mark_failed(
        self, message_id: UUID, error: str, *, consumer: str | None = None
    ) -> None:
        await self._advance(
            message_id,
            MessageStatus.FAILED,
            "message.failed",
            {"error": error},
            consumer=consumer,
        )

    async def renew_claims(self, consumer: str) -> int:
        """Heartbeat: push out the expiry of the consumer's in-flight messages."""
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(MessageRecord)
                .where(
                    MessageRecord.claimed_by == consumer,
                    MessageRecord.status.in_(_IN_FLIGHT),
                )
                .values(claim_expires_at=self._clock() + self._claim_ttl)
                .execution_options(synchronize_session=False)
            )
        return int(result.rowcount or 0)

    async def pending_count(self, agent_id: AgentId) -> int:
        async with self._sessions() as session:
            return await self._pending_count(session, agent_id.value)

    async def agents_with_pending(self) -> dict[str, int]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(MessageRecord.target_agent, func.count())
                .where(MessageRecord.status == MessageStatus.PENDING.value)
                .group_by(MessageRecord.target_agent)
            )
            return {agent: count for agent, count in rows.all()}

    async def wait_for_messages(
        self, agent_id: AgentId, timeout: float, *, stop: asyncio.Event | None = None
    ) -> bool:
        """Wait for pending messages; the DB is polled so other processes and restarts work.

        Returns False early once ``stop`` is set (call :meth:`wake` to interrupt the wait).
        """
        wakeup = self._wakeups.setdefault(agent_id.value, asyncio.Event())
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            # Clear before checking so a publish between check and wait is never lost.
            wakeup.clear()
            if stop is not None and stop.is_set():
                return False
            if await self.pending_count(agent_id) > 0:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(wakeup.wait(), min(remaining, self._poll_interval))
            except TimeoutError:
                pass

    async def requeue_in_flight(
        self, agent_ids: Collection[AgentId] | None = None, *, now: datetime | None = None
    ) -> int:
        """Return delivered/processing messages whose claim has expired to pending.

        A message without a claim expiry predates claims and counts as expired. A claim that a
        heartbeat renews while the sweep runs is left alone.
        """
        current = now if now is not None else self._clock()
        expired = or_(
            MessageRecord.claim_expires_at.is_(None), MessageRecord.claim_expires_at <= current
        )
        return await self._requeue(agent_ids, expired, "claim_expired")

    async def release_claims(self, consumer: str) -> int:
        """Requeue the consumer's own in-flight messages (graceful shutdown)."""
        return await self._requeue(None, MessageRecord.claimed_by == consumer, "released")

    async def _requeue(
        self,
        agent_ids: Collection[AgentId] | None,
        condition: ColumnElement[bool],
        reason: str,
    ) -> int:
        requeued: list[str] = []
        async with self._sessions() as session, session.begin():
            statement = select(MessageRecord).where(MessageRecord.status.in_(_IN_FLIGHT), condition)
            if agent_ids is not None:
                statement = statement.where(
                    MessageRecord.target_agent.in_([agent.value for agent in agent_ids])
                )
            for row in (await session.scalars(statement)).all():
                previous = row.status
                previous_consumer = row.claimed_by
                result = await session.execute(
                    update(MessageRecord)
                    .where(
                        MessageRecord.id == row.id,
                        MessageRecord.status == previous,
                        condition,
                    )
                    .values(
                        status=MessageStatus.PENDING.value,
                        claimed_by=None,
                        claimed_at=None,
                        claim_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    continue
                self._stage_status_event(
                    session,
                    row,
                    "message.requeued",
                    {
                        "previous_status": previous,
                        "consumer": previous_consumer,
                        "reason": reason,
                    },
                    actor=row.target_agent,
                )
                requeued.append(row.target_agent)
        for target in set(requeued):
            self._wake(target)
        return len(requeued)

    def wake(self, agent_id: AgentId) -> None:
        self._wake(agent_id.value)

    def _wake(self, agent_id: str) -> None:
        self._wakeups.setdefault(agent_id, asyncio.Event()).set()

    async def _pending_count(self, session: AsyncSession, agent_id: str) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(MessageRecord)
            .where(
                MessageRecord.target_agent == agent_id,
                MessageRecord.status == MessageStatus.PENDING.value,
            )
        )
        return int(count or 0)

    async def _advance(
        self,
        message_id: UUID,
        new_status: MessageStatus,
        event_type: str,
        payload: dict[str, object],
        *,
        consumer: str | None = None,
        renew: bool = False,
    ) -> None:
        expected = [
            old.value
            for old, allowed in _ALLOWED_MESSAGE_TRANSITIONS.items()
            if new_status in allowed
        ]
        conditions = [MessageRecord.id == message_id, MessageRecord.status.in_(expected)]
        values: dict[str, object] = {"status": new_status.value}
        if consumer is not None:
            conditions.append(MessageRecord.claimed_by == consumer)
        if renew:
            values["claim_expires_at"] = self._clock() + self._claim_ttl
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(MessageRecord)
                .where(*conditions)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            row = await session.get(MessageRecord, message_id, populate_existing=True)
            if row is None:
                raise LookupError(f"Message {message_id} does not exist")
            if result.rowcount != 1:
                raise InvalidMessageTransition(
                    f"Cannot move message {message_id} from {row.status} to {new_status.value}"
                    + (f" as {consumer}" if consumer is not None else "")
                )
            self._stage_status_event(session, row, event_type, payload, actor=row.target_agent)

    def _stage_status_event(
        self,
        session: AsyncSession,
        row: MessageRecord,
        event_type: str,
        payload: dict[str, object],
        *,
        actor: str | None = None,
    ) -> None:
        add_event(
            session,
            event_type=event_type,
            aggregate_type="message",
            aggregate_id=row.id,
            actor=actor or row.target_agent,
            payload={
                "message_id": row.id,
                "target_agent": row.target_agent,
                "message_type": row.message_type,
                **payload,
            },
            created_at=self._clock(),
            source=_SOURCE,
            objective_id=row.objective_id,
            task_id=row.task_id,
            correlation_id=row.correlation_id,
        )

    def _to_record(self, message: AgentMessage) -> MessageRecord:
        return MessageRecord(
            id=message.message_id,
            source_agent=message.source_agent.value,
            target_agent=message.target_agent.value,
            message_type=message.message_type.value,
            objective_id=message.objective_id,
            task_id=message.task_id,
            requirement_id=message.requirement_id,
            correlation_id=message.correlation_id,
            timestamp=as_utc(message.timestamp),
            payload=message.payload,
            priority=message.priority,
            deadline=as_utc(message.deadline) if message.deadline else None,
            requires_response=message.requires_response,
            status=MessageStatus.PENDING.value,
            created_at=self._clock(),
        )

    @staticmethod
    def _to_message(row: MessageRecord, status: MessageStatus) -> AgentMessage:
        return AgentMessage(
            message_id=row.id,
            source_agent=AgentId(row.source_agent),
            target_agent=AgentId(row.target_agent),
            message_type=MessageType(row.message_type),
            objective_id=row.objective_id,
            task_id=row.task_id,
            requirement_id=row.requirement_id,
            correlation_id=row.correlation_id,
            timestamp=as_utc(row.timestamp),
            payload=row.payload,
            priority=row.priority,
            deadline=as_utc(row.deadline) if row.deadline else None,
            requires_response=row.requires_response,
            status=status,
        )
