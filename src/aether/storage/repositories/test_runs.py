"""Audited independent verification runs and their individual test results."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from aether.storage.models import (
    EventCategory,
    ExecutionRunRecord,
    ResultOutcome,
    RunStatus,
    TaskRecord,
    TestResultRecord,
    TestRunRecord,
    TestVerdict,
    utcnow,
)
from aether.storage.repositories._events import add_event

__test__ = False


class InvalidTestRunTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TestResultInput:
    __test__ = False

    name: str
    outcome: ResultOutcome
    duration_ms: int | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class TestRunCounts:
    __test__ = False

    passed: int = 0
    failed: int = 0
    errors: int = 0


class _AcceptanceResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    criterion: str = Field(min_length=1)
    passed: bool = Field(strict=True)
    evidence: str = ""
    tests: list[str] | None = None


_ACCEPTANCE_RESULTS = TypeAdapter(list[_AcceptanceResult])
_STATUS_FOR_VERDICT = {
    TestVerdict.VERIFIED: RunStatus.SUCCEEDED,
    TestVerdict.FAILED: RunStatus.FAILED,
    TestVerdict.INCONCLUSIVE: RunStatus.INCONCLUSIVE,
}


class TestRunRepository:
    __test__ = False

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        *,
        suite: str,
        actor: str = "LEDGER",
        task_id: UUID | None = None,
        capability_id: UUID | None = None,
        execution_run_id: UUID | None = None,
        now: datetime | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        if not suite.strip() or len(suite) > 200:
            raise ValueError("Test suite must contain between 1 and 200 characters")
        current_time = _current_time(now)
        async with self._sessions() as session, session.begin():
            run = TestRunRecord(
                suite=suite,
                task_id=task_id,
                capability_id=capability_id,
                execution_run_id=execution_run_id,
                status=RunStatus.RUNNING,
                started_at=current_time,
            )
            session.add(run)
            await session.flush()
            add_event(
                session,
                event_type="test.run_created",
                aggregate_type="test_run",
                aggregate_id=run.id,
                actor=actor,
                payload={
                    "suite": suite,
                    "capability_id": capability_id,
                    "execution_run_id": execution_run_id,
                },
                category=EventCategory.TEST,
                correlation_id=correlation_id,
                objective_id=await _objective_id(session, run),
                task_id=task_id,
                idempotency_key=f"test.run_created:{run.id}",
            )
            return run.id

    async def add_result(
        self,
        run_id: UUID,
        *,
        name: str,
        outcome: ResultOutcome,
        duration_ms: int | None = None,
        message: str | None = None,
        actor: str = "LEDGER",
        correlation_id: UUID | None = None,
    ) -> UUID:
        [result_id] = await self.add_results(
            run_id,
            [TestResultInput(name, outcome, duration_ms, message)],
            actor=actor,
            correlation_id=correlation_id,
        )
        return result_id

    async def add_results(
        self,
        run_id: UUID,
        results: Sequence[TestResultInput],
        *,
        actor: str = "LEDGER",
        correlation_id: UUID | None = None,
    ) -> list[UUID]:
        if not results:
            raise ValueError("At least one test result is required")
        records = []
        for result in results:
            if not result.name.strip() or len(result.name) > 500:
                raise ValueError("Test result name must contain between 1 and 500 characters")
            if result.duration_ms is not None and result.duration_ms < 0:
                raise ValueError("Test result duration must be nonnegative")
            records.append(
                TestResultRecord(
                    test_run_id=run_id,
                    name=result.name,
                    outcome=ResultOutcome(result.outcome),
                    duration_ms=result.duration_ms,
                    message=result.message,
                )
            )
        async with self._sessions() as session, session.begin():
            run = await _lock_running_run(session, run_id)
            session.add_all(records)
            run.passed += sum(record.outcome == ResultOutcome.PASSED for record in records)
            run.failed += sum(record.outcome == ResultOutcome.FAILED for record in records)
            run.errors += sum(record.outcome == ResultOutcome.ERROR for record in records)
            await session.flush()
            add_event(
                session,
                event_type="test.results_added",
                aggregate_type="test_run",
                aggregate_id=run.id,
                actor=actor,
                payload={
                    "results": [
                        {
                            "result_id": record.id,
                            "name": record.name,
                            "outcome": record.outcome,
                            "duration_ms": record.duration_ms,
                        }
                        for record in records
                    ]
                },
                category=EventCategory.TEST,
                correlation_id=correlation_id,
                objective_id=await _objective_id(session, run),
                task_id=run.task_id,
            )
            return [record.id for record in records]

    async def finish(
        self,
        run_id: UUID,
        *,
        verdict: TestVerdict,
        acceptance_results: list[dict[str, Any]] | None = None,
        report_path: str | None = None,
        actor: str = "LEDGER",
        now: datetime | None = None,
        correlation_id: UUID | None = None,
        counts: TestRunCounts | None = None,
        results: Sequence[TestResultInput] = (),
        execution_run_id: UUID | None = None,
        evidence: Sequence[str] | None = None,
        source: str = "storage.repositories",
    ) -> None:
        """Record the verdict, status, criteria and (optionally) counts and results in one write.

        ``counts`` replaces the running tally; ``results`` are stored without changing counts.
        """
        verdict = TestVerdict(verdict)
        criteria = _ACCEPTANCE_RESULTS.validate_python(acceptance_results or [])
        current_time = _current_time(now)
        async with self._sessions() as session, session.begin():
            run = await _lock_running_run(session, run_id)
            if counts is not None:
                run.passed, run.failed, run.errors = counts.passed, counts.failed, counts.errors
            failing = run.failed + run.errors > 0 or any(not item.passed for item in criteria)
            if verdict is TestVerdict.VERIFIED and (not criteria or failing):
                raise ValueError(
                    "Verified runs require passing acceptance criteria and no failures"
                )
            if verdict is TestVerdict.FAILED and not failing:
                raise ValueError("Failed runs require a failing test or acceptance criterion")
            started_at = run.started_at
            if started_at.utcoffset() is None:
                started_at = started_at.replace(tzinfo=UTC)
            if current_time < started_at:
                raise ValueError("A test run cannot finish before it starts")
            run.status = _STATUS_FOR_VERDICT[verdict]
            run.verdict = verdict
            run.acceptance_results = [
                item.model_dump(mode="json", exclude_none=True) for item in criteria
            ]
            run.report_path = report_path[:1024] if report_path else None
            run.finished_at = current_time
            if execution_run_id is not None and await session.get(
                ExecutionRunRecord, execution_run_id
            ):
                run.execution_run_id = execution_run_id
            session.add_all(
                TestResultRecord(
                    test_run_id=run_id,
                    name=result.name[:500],
                    outcome=ResultOutcome(result.outcome),
                    duration_ms=result.duration_ms,
                    message=result.message,
                )
                for result in results
            )
            await session.flush()
            skipped = await session.scalar(
                select(func.count())
                .select_from(TestResultRecord)
                .where(
                    TestResultRecord.test_run_id == run_id,
                    TestResultRecord.outcome == ResultOutcome.SKIPPED,
                )
            )
            add_event(
                session,
                event_type="test.run_finished",
                aggregate_type="test_run",
                aggregate_id=run.id,
                actor=actor,
                payload={
                    "suite": run.suite,
                    "passed": run.passed,
                    "failed": run.failed,
                    "errors": run.errors,
                    "skipped": skipped,
                    "verdict": verdict,
                    "duration_seconds": (current_time - started_at).total_seconds(),
                    "evidence": list(evidence)
                    if evidence is not None
                    else [item.evidence for item in criteria if item.evidence],
                },
                category=EventCategory.TEST,
                correlation_id=correlation_id,
                objective_id=await _objective_id(session, run),
                task_id=run.task_id,
                idempotency_key=f"test.run_finished:{run.id}",
                source=source,
            )

    async def get(self, run_id: UUID) -> TestRunRecord | None:
        async with self._sessions() as session:
            return await session.get(TestRunRecord, run_id)

    async def list_results(self, run_id: UUID) -> list[TestResultRecord]:
        async with self._sessions() as session:
            results = await session.scalars(
                select(TestResultRecord)
                .where(TestResultRecord.test_run_id == run_id)
                .order_by(TestResultRecord.name, TestResultRecord.id)
            )
            return list(results)

    async def list_by_capability(self, capability_id: UUID) -> list[TestRunRecord]:
        return await self._list(TestRunRecord.capability_id == capability_id)

    async def list_by_task(self, task_id: UUID) -> list[TestRunRecord]:
        return await self._list(TestRunRecord.task_id == task_id)

    async def list_by_execution_run(self, execution_run_id: UUID) -> list[TestRunRecord]:
        return await self._list(TestRunRecord.execution_run_id == execution_run_id)

    async def _list(self, condition: ColumnElement[bool]) -> list[TestRunRecord]:
        async with self._sessions() as session:
            runs = await session.scalars(
                select(TestRunRecord)
                .where(condition)
                .order_by(TestRunRecord.started_at, TestRunRecord.id)
            )
            return list(runs)


def _current_time(now: datetime | None) -> datetime:
    value = now or utcnow()
    if value.utcoffset() is None:
        raise ValueError("Test run timestamps must include a timezone")
    return value.astimezone(UTC)


async def _lock_running_run(session: AsyncSession, run_id: UUID) -> TestRunRecord:
    run = await session.get(TestRunRecord, run_id, with_for_update=True)
    if run is None:
        raise LookupError(f"Test run {run_id} does not exist")
    if run.status not in (RunStatus.PENDING, RunStatus.RUNNING) or run.finished_at is not None:
        raise InvalidTestRunTransition(f"Test run {run_id} is already {run.status}")
    return run


async def _objective_id(session: AsyncSession, run: TestRunRecord) -> UUID | None:
    if run.task_id is None:
        return None
    task = await session.get(TaskRecord, run.task_id)
    if task is None:
        raise LookupError(f"Task {run.task_id} does not exist")
    return task.objective_id
