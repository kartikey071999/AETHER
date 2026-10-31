from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.observability.events import EVENT_CATEGORIES, EVENT_TYPES
from aether.observability.recorder import EventRecorder
from aether.storage.models import (
    ExecutionRunRecord,
    ResultOutcome,
    RunStatus,
    TestResultRecord,
    TestRunRecord,
    TestVerdict,
)
from aether.storage.repositories import (
    CapabilityRepository,
    ExecutionRunRepository,
    InvalidTestRunTransition,
    TaskRepository,
    TestResultInput,
    TestRunCounts,
    TestRunRepository,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
CRITERIA = [{"criterion": "works offline", "passed": True, "evidence": "offline test"}]


async def test_inconclusive_verdict_is_stored_with_inconclusive_status(database, objective_id):
    task_id = await TaskRepository(database.sessions).create(objective_id=objective_id, title="T")
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="offline", task_id=task_id, now=NOW)
    await repository.finish(
        run_id,
        verdict=TestVerdict.INCONCLUSIVE,
        counts=TestRunCounts(passed=1, failed=0, errors=0),
        now=NOW + timedelta(seconds=1),
    )
    run = await repository.get(run_id)
    assert run.verdict == TestVerdict.INCONCLUSIVE
    assert run.status == RunStatus.INCONCLUSIVE
    assert (run.passed, run.failed, run.errors) == (1, 0, 0)
    with pytest.raises(InvalidTestRunTransition):
        await repository.finish(run_id, verdict=TestVerdict.INCONCLUSIVE, now=NOW)


async def test_test_run_lifecycle_and_subject_queries(database, objective_id, events_for):
    task_id = await TaskRepository(database.sessions).create(objective_id=objective_id, title="OCR")
    capability_id = await CapabilityRepository(database.sessions).register(
        name="ocr", actor="SCOUT"
    )
    async with database.sessions() as session, session.begin():
        execution = await ExecutionRunRepository.stage_started(
            session, task_id=task_id, agent="FORGE", operation="test", started_at=NOW
        )
        execution_id = execution.id
    repository = TestRunRepository(database.sessions)
    correlation_id = uuid4()
    run_id = await repository.create(
        suite="offline",
        task_id=task_id,
        capability_id=capability_id,
        execution_run_id=execution_id,
        now=NOW,
        correlation_id=correlation_id,
    )
    run = await repository.get(run_id)
    assert run.status == RunStatus.RUNNING
    assert run.started_at.replace(tzinfo=UTC) == NOW
    assert run.finished_at is run.verdict is None
    events = await events_for(run_id)
    assert len(events) == 1
    assert events[0].event_type == "test.run_created"

    result_ids = await repository.add_results(
        run_id,
        [
            TestResultInput("offline", ResultOutcome.PASSED, 500),
            TestResultInput("optional", ResultOutcome.SKIPPED, message="not configured"),
        ],
        correlation_id=correlation_id,
    )
    assert len(result_ids) == 2
    assert len(await events_for(run_id)) == 2
    assert {record.id for record in await repository.list_results(run_id)} == set(result_ids)
    await repository.finish(
        run_id,
        verdict=TestVerdict.VERIFIED,
        acceptance_results=CRITERIA,
        report_path="data\\artifacts\\report.json",
        now=NOW + timedelta(seconds=2),
        correlation_id=correlation_id,
    )
    run = await repository.get(run_id)
    assert run.status == RunStatus.SUCCEEDED
    assert run.verdict == TestVerdict.VERIFIED
    assert (run.passed, run.failed, run.errors) == (1, 0, 0)
    assert run.acceptance_results == CRITERIA
    assert run.report_path == "data\\artifacts\\report.json"
    assert run.finished_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=2)
    for query, subject in [
        (repository.list_by_capability, capability_id),
        (repository.list_by_task, task_id),
        (repository.list_by_execution_run, execution_id),
    ]:
        assert [record.id for record in await query(subject)] == [run_id]
        assert await query(uuid4()) == []

    events = await events_for(run_id)
    assert [event.event_type for event in events] == [
        "test.run_created",
        "test.results_added",
        "test.run_finished",
    ]
    for event in events:
        assert event.actor == "LEDGER"
        assert event.category == EVENT_CATEGORIES[event.event_type].value == "test"
        assert event.task_id == task_id
        assert event.objective_id == objective_id
        assert event.correlation_id == correlation_id
        EVENT_TYPES[event.event_type].model_validate(event.payload)
    assert events[-1].payload == {
        "suite": "offline",
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "skipped": 1,
        "duration_seconds": 2,
        "evidence": ["offline test"],
        "verdict": "verified",
    }


@pytest.mark.parametrize("outcome", [ResultOutcome.FAILED, ResultOutcome.ERROR])
async def test_failed_or_error_tests_cannot_be_verified(database, events_for, outcome):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="unit", now=NOW)
    result_id = await repository.add_result(
        run_id, name="failure", outcome=outcome, duration_ms=1, message="assertion failed"
    )
    results = await repository.list_results(run_id)
    assert [record.id for record in results] == [result_id]
    assert results[0].message == "assertion failed"
    with pytest.raises(ValueError, match="Verified runs"):
        await repository.finish(run_id, verdict="verified", acceptance_results=CRITERIA, now=NOW)
    assert len(await events_for(run_id)) == 2
    run = await repository.get(run_id)
    assert run.finished_at is run.verdict is None
    assert run.status == RunStatus.RUNNING
    await repository.finish(run_id, verdict="failed", now=NOW)
    run = await repository.get(run_id)
    assert run.status == RunStatus.FAILED
    assert run.verdict == TestVerdict.FAILED
    assert (run.failed, run.errors) == (
        int(outcome is ResultOutcome.FAILED),
        int(outcome is ResultOutcome.ERROR),
    )
    assert len(await events_for(run_id)) == 3


async def test_failed_criterion_can_fail_a_run_without_test_failures(database):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="acceptance")
    await repository.finish(
        run_id,
        verdict="failed",
        acceptance_results=[{"criterion": "offline", "passed": False, "evidence": "network call"}],
    )
    assert (await repository.get(run_id)).verdict == TestVerdict.FAILED


async def test_test_run_clock_preserves_offset_instants(database, events_for):
    repository = TestRunRepository(database.sessions)
    offset_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    run_id = await repository.create(suite="unit", now=offset_now)
    await repository.finish(run_id, verdict="inconclusive", now=offset_now + timedelta(seconds=5))
    run = await repository.get(run_id)
    assert run.started_at.replace(tzinfo=UTC) == NOW
    assert run.finished_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=5)
    assert (await events_for(run_id))[-1].payload["duration_seconds"] == 5


@pytest.mark.parametrize("verdict", list(TestVerdict))
async def test_finished_runs_cannot_change_results_or_verdict(database, events_for, verdict):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="unit", now=NOW)
    if verdict is TestVerdict.FAILED:
        await repository.add_result(run_id, name="failure", outcome="failed")
    await repository.finish(
        run_id,
        verdict=verdict,
        acceptance_results=CRITERIA if verdict is TestVerdict.VERIFIED else None,
        now=NOW,
    )
    before = len(await events_for(run_id))
    with pytest.raises(InvalidTestRunTransition):
        await repository.add_result(run_id, name="late", outcome="passed")
    with pytest.raises(InvalidTestRunTransition):
        await repository.finish(run_id, verdict="inconclusive", now=NOW)
    assert len(await events_for(run_id)) == before
    assert (await repository.get(run_id)).verdict == verdict


@pytest.mark.parametrize(
    "values",
    [
        {"verdict": "verified"},
        {"verdict": "verified", "acceptance_results": [{"criterion": "offline", "passed": False}]},
        {"verdict": "failed", "acceptance_results": CRITERIA},
        {"verdict": "unknown"},
        {"verdict": "verified", "acceptance_results": [{"criterion": "x", "passed": "yes"}]},
        {"verdict": "inconclusive", "now": NOW - timedelta(seconds=1)},
    ],
)
async def test_invalid_finish_does_not_change_run(database, events_for, values):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="unit", now=NOW)
    with pytest.raises(ValueError):
        await repository.finish(run_id, **{"now": NOW, **values})
    assert len(await events_for(run_id)) == 1
    assert (await repository.get(run_id)).status == RunStatus.RUNNING


@pytest.mark.parametrize("values", [{"name": " "}, {"outcome": "unknown"}, {"duration_ms": -1}])
async def test_invalid_results_do_not_change_counts(database, events_for, values):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="unit")
    with pytest.raises(ValueError):
        await repository.add_result(run_id, **{"name": "case", "outcome": "passed", **values})
    with pytest.raises(ValueError, match="At least one"):
        await repository.add_results(run_id, [])
    assert await repository.list_results(run_id) == []
    assert len(await events_for(run_id)) == 1


async def test_test_runs_handle_unknown_ids_and_invalid_creation(database):
    repository = TestRunRepository(database.sessions)
    with pytest.raises(ValueError):
        await repository.create(suite=" ")
    with pytest.raises(ValueError, match="timezone"):
        await repository.create(suite="unit", now=NOW.replace(tzinfo=None))
    assert await repository.get(uuid4()) is None
    assert await repository.list_results(uuid4()) == []
    with pytest.raises(LookupError):
        await repository.add_result(uuid4(), name="case", outcome="passed")
    with pytest.raises(LookupError):
        await repository.finish(uuid4(), verdict="inconclusive")


@pytest.mark.parametrize("operation", ["create", "add", "finish"])
async def test_test_run_changes_roll_back_with_their_event(
    database, events_for, monkeypatch, operation
):
    repository = TestRunRepository(database.sessions)
    run_id = await repository.create(suite="unit", now=NOW)

    def fail(self, session, event):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(EventRecorder, "add", fail)
    with pytest.raises(RuntimeError, match="event store"):
        if operation == "create":
            await repository.create(suite="other", now=NOW)
        elif operation == "add":
            await repository.add_result(run_id, name="case", outcome="passed")
        else:
            await repository.finish(run_id, verdict="inconclusive", now=NOW)
    run = await repository.get(run_id)
    assert run.status == RunStatus.RUNNING
    assert run.verdict is run.finished_at is None
    assert run.passed == 0
    async with database.sessions() as session:
        assert list(await session.scalars(select(TestResultRecord))) == []
        assert [record.id for record in await session.scalars(select(TestRunRecord))] == [run_id]
        assert list(await session.scalars(select(ExecutionRunRecord))) == []
    assert len(await events_for(run_id)) == 1
