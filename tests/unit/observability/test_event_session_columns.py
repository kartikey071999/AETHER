from uuid import uuid4

from sqlalchemy import select

from aether.observability.events import AetherEvent, build_event
from aether.observability.recorder import EventRecorder
from aether.storage.database import Database
from aether.storage.models import EventRecord


def _event(**extra: object) -> AetherEvent:
    return build_event(
        "error.raised",
        source="unit-test",
        actor="system",
        aggregate_type="runtime",
        aggregate_id=uuid4(),
        payload={"error_type": "ExampleError", "message": "safe diagnostic"},
        **extra,
    )


async def test_recorder_persists_session_id_and_branch(database: Database) -> None:
    session_id = uuid4()
    event = _event(session_id=session_id, branch="feature/event-session-columns")

    await EventRecorder().record(database.sessions, event)

    async with database.sessions() as session:
        stored = await session.scalar(select(EventRecord).where(EventRecord.id == event.event_id))
        by_session = await session.scalars(
            select(EventRecord.id).where(EventRecord.session_id == str(session_id))
        )
        assert list(by_session) == [event.event_id]
    assert stored is not None
    assert stored.session_id == str(session_id)
    assert stored.branch == "feature/event-session-columns"


async def test_session_id_and_branch_default_to_null(database: Database) -> None:
    event = _event()

    async with database.sessions() as session, session.begin():
        EventRecorder().add(session, event)

    async with database.sessions() as session:
        stored = await session.get(EventRecord, event.event_id)
    assert stored is not None
    assert stored.session_id is None
    assert stored.branch is None
