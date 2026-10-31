import asyncio
from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.communication.bus import InvalidMessageTransition, MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageStatus, MessageType
from aether.storage.models import EventRecord, MessageRecord


def make_message(**overrides) -> AgentMessage:
    values = {
        "source_agent": AgentId.ORCHESTR,
        "target_agent": AgentId.FORGE,
        "message_type": MessageType.TASK_ASSIGN,
        "payload": {"hello": "world"},
    }
    values.update(overrides)
    return AgentMessage(**values)


async def events_for(sessions, message_id) -> Counter[str]:
    async with sessions() as session:
        rows = await session.scalars(
            select(EventRecord).where(EventRecord.aggregate_id == message_id)
        )
        return Counter(event.event_type for event in rows)


async def test_publish_claim_and_acknowledge_lifecycle(sessions) -> None:
    bus = MessageBus(sessions)
    message = make_message()

    assert await bus.publish(message) is True
    claimed = await bus.claim(AgentId.FORGE, 5, consumer="c1")
    assert [item.message_id for item in claimed] == [message.message_id]
    assert claimed[0].status is MessageStatus.DELIVERED
    assert claimed[0].payload == {"hello": "world"}
    assert await bus.claim(AgentId.FORGE, 5, consumer="c2") == []

    await bus.mark_processing(message.message_id, consumer="c1")
    await bus.mark_processed(message.message_id)

    async with sessions() as session:
        record = await session.get(MessageRecord, message.message_id)
    assert record is not None and record.status == "processed"
    assert await events_for(sessions, message.message_id) == Counter(
        {
            "message.published": 1,
            "message.delivered": 1,
            "message.processing": 1,
            "message.processed": 1,
        }
    )


async def test_mark_failed_records_error(sessions) -> None:
    bus = MessageBus(sessions)
    message = make_message()
    await bus.publish(message)
    await bus.claim(AgentId.FORGE, 1, consumer="c")
    await bus.mark_processing(message.message_id)
    await bus.mark_failed(message.message_id, "boom")

    async with sessions() as session:
        event = await session.scalar(
            select(EventRecord).where(EventRecord.event_type == "message.failed")
        )
    assert event is not None and event.payload["error"] == "boom"


async def test_invalid_transitions_are_rejected(sessions) -> None:
    bus = MessageBus(sessions)
    message = make_message()
    await bus.publish(message)

    with pytest.raises(InvalidMessageTransition):
        await bus.mark_processed(message.message_id)
    with pytest.raises(InvalidMessageTransition):
        await bus.mark_processing(message.message_id)
    with pytest.raises(LookupError):
        await bus.mark_processing(uuid4())

    await bus.claim(AgentId.FORGE, 1, consumer="c")
    await bus.mark_processing(message.message_id)
    await bus.mark_processed(message.message_id)
    with pytest.raises(InvalidMessageTransition):
        await bus.mark_failed(message.message_id, "late")


async def test_republishing_same_message_id_is_idempotent(sessions) -> None:
    bus = MessageBus(sessions)
    message = make_message()

    assert await bus.publish(message) is True
    assert await bus.publish(message) is False

    async with sessions() as session:
        records = (await session.scalars(select(MessageRecord))).all()
    assert len(records) == 1
    assert (await events_for(sessions, message.message_id))["message.published"] == 1


async def test_publish_losing_a_concurrent_insert_race_reports_not_created(
    sessions, monkeypatch
) -> None:
    """A rival publisher commits the same id between our existence check and our insert."""
    message = make_message()
    assert await MessageBus(sessions).publish(message) is True
    original_get = AsyncSession.get
    misses = []

    async def stale_get(self, entity, ident, *args, **kwargs):
        if entity is MessageRecord and not misses:
            misses.append(ident)
            return None  # the check ran before the rival's commit became visible
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", stale_get)

    assert await MessageBus(sessions).publish(message) is False
    assert misses == [message.message_id]
    async with sessions() as session:
        records = (await session.scalars(select(MessageRecord))).all()
    assert len(records) == 1
    assert (await events_for(sessions, message.message_id))["message.published"] == 1


async def test_claim_is_targeted_priority_ordered_and_limited(sessions) -> None:
    bus = MessageBus(sessions)
    low = make_message(priority=10)
    high = make_message(priority=90)
    other = make_message(target_agent=AgentId.SCOUT)
    for message in (low, high, other):
        await bus.publish(message)

    first = await bus.claim(AgentId.FORGE, 1, consumer="c")
    rest = await bus.claim(AgentId.FORGE, 10, consumer="c")

    assert [m.message_id for m in first] == [high.message_id]
    assert [m.message_id for m in rest] == [low.message_id]
    assert await bus.pending_count(AgentId.SCOUT) == 1


async def test_concurrent_claims_never_deliver_a_message_twice(sessions) -> None:
    bus = MessageBus(sessions)
    for _ in range(6):
        await bus.publish(make_message())

    batches = await asyncio.gather(
        *(bus.claim(AgentId.FORGE, 3, consumer=f"c{i}") for i in range(3))
    )
    # Racing claimers may each lose some candidates; a final sweep collects the leftovers.
    batches.append(await bus.claim(AgentId.FORGE, 10, consumer="sweeper"))

    ids = [message.message_id for batch in batches for message in batch]
    assert len(ids) == len(set(ids)) == 6


async def test_new_bus_instance_reconstructs_pending_work_from_database(sessions) -> None:
    pending = make_message()
    in_flight = make_message()
    done = make_message()
    first_bus = MessageBus(sessions)
    for message in (pending, in_flight, done):
        await first_bus.publish(message)
    claimed = await first_bus.claim(AgentId.FORGE, 2, consumer="dead")
    assert len(claimed) == 2

    restarted = MessageBus(sessions)
    assert await restarted.pending_count(AgentId.FORGE) == 1
    assert await restarted.requeue_in_flight([AgentId.FORGE]) == 2
    assert await restarted.agents_with_pending() == {"FORGE": 3}
    recovered = await restarted.claim(AgentId.FORGE, 10, consumer="new")
    assert {m.message_id for m in recovered} == {
        pending.message_id,
        in_flight.message_id,
        done.message_id,
    }


async def test_wait_for_messages_wakes_on_publish_and_times_out(sessions) -> None:
    bus = MessageBus(sessions, poll_interval=5.0)

    assert await bus.wait_for_messages(AgentId.FORGE, 0.05) is False

    waiter = asyncio.create_task(bus.wait_for_messages(AgentId.FORGE, 3.0))
    await asyncio.sleep(0.05)
    await bus.publish(make_message())
    assert await asyncio.wait_for(waiter, 1.0) is True


async def test_wait_for_messages_returns_early_once_stop_is_set(sessions) -> None:
    bus = MessageBus(sessions, poll_interval=5.0)
    stop = asyncio.Event()

    waiter = asyncio.create_task(bus.wait_for_messages(AgentId.FORGE, 30.0, stop=stop))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    stop.set()
    bus.wake(AgentId.FORGE)

    assert await asyncio.wait_for(waiter, 1.0) is False
    # A stop that is already set never waits, even with no wake-up.
    already_stopped = bus.wait_for_messages(AgentId.FORGE, 30.0, stop=stop)
    assert await asyncio.wait_for(already_stopped, 1.0) is False


async def test_wait_for_messages_polls_database_for_other_processes(sessions) -> None:
    consumer_bus = MessageBus(sessions, poll_interval=0.02)
    producer_bus = MessageBus(sessions)

    waiter = asyncio.create_task(consumer_bus.wait_for_messages(AgentId.FORGE, 2.0))
    await asyncio.sleep(0.05)
    await producer_bus.publish(make_message())

    assert await asyncio.wait_for(waiter, 1.0) is True
