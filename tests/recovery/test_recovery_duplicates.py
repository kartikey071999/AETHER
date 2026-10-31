"""Scenario 7: the same message, event or task arrives more than once.

Publishing is idempotent by message id, events by id and idempotency key (first write wins),
and tasks by their idempotency key, so retries after an ambiguous failure, or redelivery after
a crash, never create a second copy. A delivered message is handled once per delivery and a
processed message is never redelivered.
"""

import asyncio
from uuid import uuid4

import pytest
from reliability_support import (
    RecordingAgent,
    assign_message,
    build_engine,
    event_counts,
    message_status,
    wait_until,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.storage.models import EventRecord, MessageRecord, TaskRecord
from aether.storage.repositories import TaskRepository


async def _count(sessions, model, *criteria) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(model).where(*criteria))


async def test_republishing_a_message_is_a_noop(sessions) -> None:
    bus = MessageBus(sessions)
    message = assign_message()

    assert await bus.publish(message)
    assert not await bus.publish(message)
    assert not await bus.publish(message.model_copy(update={"payload": {"changed": True}}))

    assert await _count(sessions, MessageRecord) == 1
    async with sessions() as session:
        stored = await session.get(MessageRecord, message.message_id)
    assert stored is not None and stored.payload == {}  # first write wins
    assert (await event_counts(sessions, message.message_id))["message.published"] == 1


async def test_concurrent_duplicate_publishes_store_one_message(sessions) -> None:
    message = assign_message()

    created = await asyncio.gather(*(MessageBus(sessions).publish(message) for _ in range(4)))

    assert sorted(created) == [False, False, False, True]
    assert await _count(sessions, MessageRecord) == 1
    assert (await event_counts(sessions, message.message_id))["message.published"] == 1


async def test_duplicate_events_are_first_write_wins(sessions) -> None:
    recorder = EventRecorder()
    aggregate_id = uuid4()

    def event(message: str):
        return build_event(
            "error.raised",
            source="recovery-test",
            actor="system",
            aggregate_type="runtime",
            aggregate_id=aggregate_id,
            payload={"error_type": "Duplicate", "message": message, "details": {}},
            idempotency_key="recovery-test:error-1",
        )

    original = event("first")
    stored = await recorder.record(sessions, original)
    retry_same_id = await recorder.record(sessions, original)
    retry_same_key = await recorder.record(sessions, event("second"))

    assert stored.id == retry_same_id.id == retry_same_key.id == original.event_id
    assert retry_same_key.payload["message"] == "first"
    assert await _count(sessions, EventRecord) == 1


async def test_task_creation_is_idempotent_by_key(sessions, objective_id) -> None:
    tasks = TaskRepository(sessions)
    key = f"objective:{objective_id}:step-1"
    await tasks.create(objective_id=objective_id, title="step 1", idempotency_key=key)

    with pytest.raises(IntegrityError):
        await tasks.create(objective_id=objective_id, title="step 1 again", idempotency_key=key)

    assert await _count(sessions, TaskRecord, TaskRecord.idempotency_key == key) == 1
    assert await _count(sessions, EventRecord, EventRecord.event_type == "task.created") == 1


async def test_engine_handles_a_message_once_even_if_it_is_published_twice(sessions) -> None:
    agent = RecordingAgent()
    engine = build_engine(sessions, [agent])
    message = assign_message()

    await engine.start()
    try:
        assert await engine.bus.publish(message)
        await wait_until(lambda: _status_is(sessions, message.message_id, "processed"))
        assert not await engine.bus.publish(message)  # a late duplicate from a retrying sender
        await asyncio.sleep(0.2)
    finally:
        await engine.stop()

    assert agent.handled == [message.message_id]


async def test_processed_messages_are_not_redelivered_after_restart(sessions) -> None:
    message = assign_message()
    first_agent = RecordingAgent()
    first = build_engine(sessions, [first_agent])
    await first.start()
    try:
        await first.bus.publish(message)
        await wait_until(lambda: _status_is(sessions, message.message_id, "processed"))
    finally:
        await first.stop()

    second_agent = RecordingAgent()
    second = build_engine(sessions, [second_agent])
    await second.start()
    try:
        await asyncio.sleep(0.2)
        assert second.recovery_report is not None
    finally:
        await second.stop()

    assert first_agent.handled == [message.message_id]
    assert second_agent.handled == []
    counts = await event_counts(sessions, message.message_id)
    assert counts["message.processed"] == 1 and "message.requeued" not in counts


async def test_two_consumers_claiming_the_same_queue_never_share_a_message(sessions) -> None:
    bus = MessageBus(sessions)
    messages = [assign_message() for _ in range(10)]
    for message in messages:
        await bus.publish(message)

    batches = await asyncio.gather(
        *(MessageBus(sessions).claim(AgentId.FORGE, 10, consumer=f"consumer-{n}") for n in range(3))
    )

    claimed = [message.message_id for batch in batches for message in batch]
    assert sorted(claimed) == sorted(message.message_id for message in messages)
    assert len(set(claimed)) == len(claimed)


async def _status_is(sessions, message_id, status: str) -> bool:
    return await message_status(sessions, message_id) == status
