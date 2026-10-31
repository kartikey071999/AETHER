"""LEDGER's durable test-run records: ``test_runs``/``test_results`` plus the audit event.

No shared repository exists for these tables yet, so LEDGER owns this small store. Every write
happens in one transaction together with its ``test.run_finished`` event, and finishing a run
twice is a no-op, so redelivered messages never duplicate evidence.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.contracts import TestCaseResult, TestCounts
from aether.communication.messages import AgentId
from aether.observability.recorder import stage_event
from aether.storage.models import (
    CapabilityRecord,
    ExecutionRunRecord,
    RunStatus,
    TaskRecord,
    TestResultRecord,
    TestRunRecord,
)

_ACTOR = AgentId.LEDGER.value
_SOURCE = "agents.ledger"
_OPEN = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


async def existing_id(
    session: AsyncSession, model: type[Any], identifier: UUID | None
) -> UUID | None:
    """``identifier`` when that row exists; foreign keys must never point at missing rows."""
    if identifier is None:
        return None
    return identifier if await session.get(model, identifier) is not None else None


class TestRunStore:
    __test__ = False

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, run_id: UUID) -> TestRunRecord | None:
        async with self._sessions() as session:
            return await session.get(TestRunRecord, run_id)

    async def results(self, run_id: UUID) -> list[TestResultRecord]:
        statement = select(TestResultRecord).where(TestResultRecord.test_run_id == run_id)
        async with self._sessions() as session:
            return list(await session.scalars(statement.order_by(TestResultRecord.name)))

    async def open_runs(self) -> list[TestRunRecord]:
        statement = select(TestRunRecord).where(TestRunRecord.status.in_(_OPEN))
        async with self._sessions() as session:
            return list(await session.scalars(statement.order_by(TestRunRecord.started_at)))

    async def finished_for_capability(self, capability_id: UUID) -> list[TestRunRecord]:
        """Finished runs for a capability, oldest first."""
        statement = select(TestRunRecord).where(
            TestRunRecord.capability_id == capability_id,
            TestRunRecord.status.not_in(_OPEN),
        )
        async with self._sessions() as session:
            records = await session.scalars(
                statement.order_by(TestRunRecord.finished_at, TestRunRecord.started_at)
            )
            return list(records)

    async def open_run(
        self,
        run_id: UUID,
        *,
        suite: str,
        task_id: UUID | None,
        capability_id: UUID | None,
        execution_run_id: UUID | None,
        criteria: Sequence[str],
        now: datetime,
    ) -> TestRunRecord:
        """Create the pending run for ``run_id`` unless it already exists (redelivery)."""
        async with self._sessions() as session, session.begin():
            record = await session.get(TestRunRecord, run_id)
            if record is not None:
                session.expunge(record)
                return record
            record = TestRunRecord(
                id=run_id,
                task_id=await existing_id(session, TaskRecord, task_id),
                capability_id=await existing_id(session, CapabilityRecord, capability_id),
                execution_run_id=await existing_id(session, ExecutionRunRecord, execution_run_id),
                suite=suite[:200],
                status=RunStatus.PENDING.value,
                passed=0,
                failed=0,
                errors=0,
                acceptance_results=[
                    {"criterion": criterion, "passed": False, "evidence": "not yet verified"}
                    for criterion in criteria
                ],
                started_at=now,
            )
            session.add(record)
            await session.flush()
            session.expunge(record)
            return record

    async def finish(
        self,
        run_id: UUID,
        *,
        status: RunStatus,
        counts: TestCounts,
        cases: Sequence[TestCaseResult],
        acceptance_results: list[dict[str, Any]],
        execution_run_id: UUID | None,
        report_path: str | None,
        duration_seconds: float,
        evidence: list[str],
        task_id: UUID | None,
        correlation_id: UUID | None,
        now: datetime,
    ) -> bool:
        """Record the verdict once; returns False when the run had already finished."""
        async with self._sessions() as session, session.begin():
            statement = select(TestRunRecord).where(TestRunRecord.id == run_id)
            record = await session.scalar(statement.with_for_update())
            if record is None:
                raise LookupError(f"test run {run_id} does not exist")
            if record.status not in _OPEN:
                return False
            linked_run = await existing_id(session, ExecutionRunRecord, execution_run_id)
            record.status = status.value
            record.passed = counts.passed
            record.failed = counts.failed
            record.errors = counts.errors
            record.acceptance_results = acceptance_results
            record.execution_run_id = linked_run or record.execution_run_id
            record.report_path = report_path[:1024] if report_path else None
            record.finished_at = now
            for case in cases:
                session.add(
                    TestResultRecord(
                        test_run_id=run_id,
                        name=case.name[:500],
                        outcome=case.outcome.value,
                        duration_ms=case.duration_ms,
                        message=case.message,
                    )
                )
            stage_event(
                session,
                "test.run_finished",
                source=_SOURCE,
                actor=_ACTOR,
                aggregate_type="test_run",
                aggregate_id=run_id,
                payload={
                    "suite": record.suite,
                    "passed": counts.passed,
                    "failed": counts.failed + counts.errors,
                    "skipped": counts.skipped,
                    "duration_seconds": max(duration_seconds, 0.0),
                    "evidence": evidence,
                },
                task_id=task_id,
                correlation_id=correlation_id,
                idempotency_key=f"test.run_finished:{run_id}",
            )
            return True
