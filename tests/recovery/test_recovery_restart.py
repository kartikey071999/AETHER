"""Scenario 1: a restart rebuilds runtime state from the database without duplicate execution.

Spec section 21 ("Persistence and recovery"): tasks, messages and objectives live in the
database, so a new process continues exactly where the old one stopped. Work that already
finished is never repeated; work that was in flight is resumed once.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from reliability_support import (
    RecordingAgent,
    assign_message,
    build_engine,
    event_counts,
    events_of,
    get_task,
    make_task,
    message_status,
    task_state,
    wait_until,
)
from sqlalchemy import func, select, update

from aether.communication.messages import AgentId, MessageType
from aether.runtime.scenario import CapabilityScenario, ScenarioTiming, scenario_settings
from aether.storage.database import Database
from aether.storage.models import MessageRecord, ObjectiveRecord, TaskRecord, TaskStatus

DEAD_OWNER = "engine-deadbeef"


async def _completed(sessions, *task_ids: UUID) -> bool:
    return all([await task_state(sessions, t) is TaskStatus.COMPLETED for t in task_ids])


async def _processed(sessions, *message_ids: UUID) -> bool:
    return all([await message_status(sessions, m) == "processed" for m in message_ids])


async def test_restart_resumes_pending_and_in_flight_work_exactly_once(
    sessions, objective_id
) -> None:
    past = datetime.now(UTC) - timedelta(minutes=5)
    # The previous process died with one task leased, one queued and one already finished.
    in_flight = await make_task(
        sessions,
        objective_id,
        state=TaskStatus.RUNNING,
        attempt=1,
        lease_owner=f"{DEAD_OWNER}/x",
        lease_expires_at=past,
        title="in flight",
    )
    queued = await make_task(sessions, objective_id, title="queued")
    finished = await make_task(sessions, objective_id, state=TaskStatus.COMPLETED, title="done")

    # The dead consumer had fully handled `done`, was handling `processing` and had only
    # claimed `delivered`; `fresh` was published while no engine was running.
    bus = build_engine(sessions, [RecordingAgent()]).bus
    done, processing, delivered = (assign_message() for _ in range(3))
    for message in (done, processing, delivered):
        assert await bus.publish(message)
    claimed = await bus.claim(AgentId.FORGE, 3, consumer=DEAD_OWNER)
    assert len(claimed) == 3
    for message in (done, processing):
        await bus.mark_processing(message.message_id, consumer=DEAD_OWNER)
    await bus.mark_processed(done.message_id)
    fresh = assign_message()
    await bus.publish(fresh)

    agent = RecordingAgent()
    engine = build_engine(sessions, [agent])
    await engine.start()
    try:
        report = engine.recovery_report
        assert report is not None
        assert [outcome.task_id for outcome in report.recovered_tasks] == [in_flight]
        assert report.requeued_messages == 2
        await wait_until(lambda: _completed(sessions, in_flight, queued))
        await wait_until(
            lambda: _processed(
                sessions, processing.message_id, delivered.message_id, fresh.message_id
            )
        )
    finally:
        await engine.stop()

    assert sorted(agent.executed) == sorted([in_flight, queued])
    assert sorted(agent.handled) == sorted(
        [processing.message_id, delivered.message_id, fresh.message_id]
    )
    assert await task_state(sessions, finished) is TaskStatus.COMPLETED
    recovered = await get_task(sessions, in_flight)
    assert recovered.attempt == 2  # the crashed attempt counts; the retry is attempt 2
    for task_id in (in_flight, queued):
        assert (await event_counts(sessions, task_id))["task.completed"] == 1
    [expired] = await events_of(sessions, "task.lease_expired", in_flight)
    assert expired.payload["owner"] == f"{DEAD_OWNER}/x"

    # A second restart finds nothing left to do: nothing runs or is delivered twice.
    again = RecordingAgent()
    second = build_engine(sessions, [again])
    await second.start()
    try:
        assert second.recovery_report is not None
        assert second.recovery_report.recovered_tasks == []
        assert second.recovery_report.requeued_messages == 0
        assert await second.schedule_once() == 0
    finally:
        await second.stop()
    assert again.executed == [] and again.handled == []


async def test_restart_keeps_live_leases_of_other_owners(sessions, objective_id) -> None:
    """Only expired leases are recovered: a lease still within its TTL is left alone."""
    live = await make_task(
        sessions,
        objective_id,
        state=TaskStatus.RUNNING,
        attempt=1,
        lease_owner=f"{DEAD_OWNER}/live",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    agent = RecordingAgent()
    engine = build_engine(sessions, [agent])
    await engine.start()
    try:
        assert engine.recovery_report is not None
        assert engine.recovery_report.recovered_tasks == []
        assert await engine.schedule_once() == 0
    finally:
        await engine.stop()
    task = await get_task(sessions, live)
    assert task.state == TaskStatus.RUNNING and task.lease_owner == f"{DEAD_OWNER}/live"
    assert agent.executed == []


# -- the full runtime: an objective redelivered after a crash --------------------------------

FAST = ScenarioTiming(
    tick_seconds=0.2, poll_seconds=0.05, recovery_seconds=1.0, shutdown_seconds=0.5
)


async def _objective_rows(database: Database, title_prefix: str) -> list[ObjectiveRecord]:
    async with database.sessions() as session:
        rows = await session.scalars(
            select(ObjectiveRecord).where(ObjectiveRecord.title.startswith(title_prefix))
        )
        return list(rows)


async def _duplicate_task_keys(database: Database, objective_id: UUID) -> list[str]:
    async with database.sessions() as session:
        rows = await session.execute(
            select(TaskRecord.idempotency_key, func.count())
            .where(
                TaskRecord.objective_id == objective_id,
                TaskRecord.idempotency_key.is_not(None),
            )
            .group_by(TaskRecord.idempotency_key)
            .having(func.count() > 1)
        )
        return [key for key, _ in rows.all()]


async def _create_message(database: Database) -> MessageRecord:
    async with database.sessions() as session:
        [row] = list(
            await session.scalars(
                select(MessageRecord).where(
                    MessageRecord.message_type == MessageType.OBJECTIVE_CREATE.value
                )
            )
        )
    return row


async def test_redelivered_objective_after_crash_is_created_and_planned_once(
    tmp_path: Path,
) -> None:
    settings = scenario_settings(tmp_path)
    first = CapabilityScenario(settings, timing=FAST)
    async with first:
        objective_id = await first.submit_objective(tag="redelivery")
        runtime = first.runtime
        assert runtime is not None

        async def planned() -> bool:
            async with runtime.database.sessions() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(TaskRecord)
                    .where(TaskRecord.objective_id == objective_id)
                )
            return bool(count)

        await wait_until(planned, timeout=30.0)
        await wait_until(lambda: _message_processed(runtime.database), timeout=30.0)

    # The process "died" after ORCHESTR acted but before the bus recorded the outcome.
    database = Database(settings.database_url.get_secret_value())
    try:
        message = await _create_message(database)
        async with database.sessions() as session, session.begin():
            await session.execute(
                update(MessageRecord)
                .where(MessageRecord.id == message.id)
                .values(status="processing")
            )
        titles = [row.title for row in await _objective_rows(database, "")]
    finally:
        await database.close()

    second = CapabilityScenario(settings, timing=FAST, create_schema=False)
    async with second:
        runtime = second.runtime
        assert runtime is not None
        report = runtime.engine.recovery_report
        assert report is not None and report.requeued_messages >= 1
        await wait_until(lambda: _message_processed(runtime.database), timeout=30.0)
        objectives = await _objective_rows(runtime.database, "")
        assert sorted(row.title for row in objectives) == sorted(titles)
        assert [row.id for row in objectives] == [objective_id]
        assert await _duplicate_task_keys(runtime.database, objective_id) == []
        requeued = await events_of(runtime.database.sessions, "message.requeued", message.id)
        assert len(requeued) == 1


async def _message_processed(database: Database) -> bool:
    return (await _create_message(database)).status == "processed"
