"""Message claims: a consumer owns what it claimed until the claim expires (T-051)."""

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from reliability_support import FakeClock
from sqlalchemy import select

from aether.communication.bus import InvalidMessageTransition, MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.storage.models import EventRecord, MessageRecord

TTL = timedelta(seconds=60)


def make_message() -> AgentMessage:
    return AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.TASK_ASSIGN,
        payload={},
    )


async def row(sessions, message_id: UUID) -> MessageRecord:
    async with sessions() as session:
        record = await session.get(MessageRecord, message_id, populate_existing=True)
    assert record is not None
    return record


async def requeue_events(sessions, message_id: UUID) -> list[EventRecord]:
    async with sessions() as session:
        rows = await session.scalars(
            select(EventRecord).where(
                EventRecord.aggregate_id == message_id,
                EventRecord.event_type == "message.requeued",
            )
        )
        return list(rows)


async def test_claim_records_consumer_and_expiry(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)

    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")

    record = await row(sessions, message.message_id)
    assert record.claimed_by == "engine-a"
    assert record.claim_expires_at is not None
    assert record.claim_expires_at.replace(tzinfo=None) == (clock.now + TTL).replace(tzinfo=None)


async def test_live_claims_are_not_requeued_but_expired_ones_are(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")
    await bus.mark_processing(message.message_id, consumer="engine-a")

    clock.advance(59)
    assert await bus.requeue_in_flight([AgentId.FORGE]) == 0
    assert (await row(sessions, message.message_id)).status == "processing"

    clock.advance(2)
    assert await bus.requeue_in_flight([AgentId.FORGE]) == 1
    record = await row(sessions, message.message_id)
    assert (record.status, record.claimed_by, record.claim_expires_at) == ("pending", None, None)
    [event] = await requeue_events(sessions, message.message_id)
    assert event.payload["consumer"] == "engine-a"
    assert event.payload["reason"] == "claim_expired"
    assert event.payload["previous_status"] == "processing"


async def test_heartbeat_keeps_a_claim_alive(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")

    for _ in range(5):
        clock.advance(40)
        assert await bus.renew_claims("engine-a") == 1
        assert await bus.requeue_in_flight() == 0

    assert (await row(sessions, message.message_id)).status == "delivered"


async def test_heartbeat_only_renews_the_callers_claims(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    mine, theirs = make_message(), make_message()
    await bus.publish(mine)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")
    await bus.publish(theirs)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-b")

    clock.advance(45)
    assert await bus.renew_claims("engine-a") == 1
    clock.advance(30)

    assert await bus.requeue_in_flight() == 1  # engine-b never renewed
    assert (await row(sessions, mine.message_id)).status == "delivered"
    assert (await row(sessions, theirs.message_id)).status == "pending"


async def test_claims_without_an_expiry_are_treated_as_expired(sessions) -> None:
    """Rows written before claims existed have no expiry; their consumer is long gone."""
    bus = MessageBus(sessions, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="legacy")
    async with sessions() as session, session.begin():
        record = await session.get(MessageRecord, message.message_id)
        assert record is not None
        record.claim_expires_at = None
        record.claimed_by = None

    assert await bus.requeue_in_flight() == 1
    assert (await row(sessions, message.message_id)).status == "pending"


async def test_release_claims_requeues_only_the_consumers_messages(sessions) -> None:
    bus = MessageBus(sessions, claim_ttl=TTL)
    mine, theirs = make_message(), make_message()
    await bus.publish(mine)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")
    await bus.publish(theirs)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-b")

    assert await bus.release_claims("engine-a") == 1

    assert (await row(sessions, mine.message_id)).status == "pending"
    assert (await row(sessions, theirs.message_id)).status == "delivered"
    [event] = await requeue_events(sessions, mine.message_id)
    assert event.payload["reason"] == "released"


async def test_a_consumer_that_lost_its_claim_cannot_finish_the_message(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")
    await bus.mark_processing(message.message_id, consumer="engine-a")
    clock.advance(120)
    assert await bus.requeue_in_flight() == 1
    [taken] = await bus.claim(AgentId.FORGE, 1, consumer="engine-b")
    assert taken.message_id == message.message_id

    with pytest.raises(InvalidMessageTransition):
        await bus.mark_processed(message.message_id, consumer="engine-a")
    with pytest.raises(InvalidMessageTransition):
        await bus.mark_failed(message.message_id, "late", consumer="engine-a")
    with pytest.raises(InvalidMessageTransition):
        await bus.mark_processing(message.message_id, consumer="engine-a")

    await bus.mark_processing(message.message_id, consumer="engine-b")
    await bus.mark_processed(message.message_id, consumer="engine-b")
    assert (await row(sessions, message.message_id)).status == "processed"


async def test_processing_renews_the_claim(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="engine-a")

    clock.advance(50)
    await bus.mark_processing(message.message_id, consumer="engine-a")
    clock.advance(50)

    assert await bus.requeue_in_flight() == 0


async def test_concurrent_sweeps_requeue_each_expired_message_once(sessions) -> None:
    clock = FakeClock()
    bus = MessageBus(sessions, clock=clock, claim_ttl=TTL)
    messages = [make_message() for _ in range(6)]
    for message in messages:
        await bus.publish(message)
    await bus.claim(AgentId.FORGE, 6, consumer="engine-a")
    clock.advance(120)

    counts = await asyncio.gather(*(bus.requeue_in_flight() for _ in range(3)))

    assert sum(counts) == 6
    for message in messages:
        assert len(await requeue_events(sessions, message.message_id)) == 1


def test_claim_ttl_must_be_positive(sessions) -> None:
    with pytest.raises(ValueError):
        MessageBus(sessions, claim_ttl=timedelta(0))
