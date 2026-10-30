"""Execution run records written by the safe execution adapters.

Staging methods take the caller's ``AsyncSession`` so each state change and its typed audit
event commit together.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import ExecutionRunRecord, RunStatus


class ExecutionRunRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, run_id: UUID) -> ExecutionRunRecord | None:
        async with self._sessions() as session:
            return await session.get(ExecutionRunRecord, run_id)

    @staticmethod
    async def stage_started(
        session: AsyncSession,
        *,
        agent: str,
        operation: str,
        started_at: datetime,
        run_id: UUID | None = None,
        task_id: UUID | None = None,
        security_decision_id: UUID | None = None,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> ExecutionRunRecord:
        record = ExecutionRunRecord(
            id=run_id or uuid4(),
            task_id=task_id,
            agent=agent,
            operation=operation,
            status=RunStatus.RUNNING,
            security_decision_id=security_decision_id,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            resource_usage={},
            started_at=started_at,
        )
        session.add(record)
        await session.flush()
        return record

    @staticmethod
    async def stage_finished(
        session: AsyncSession,
        run_id: UUID,
        *,
        status: RunStatus,
        finished_at: datetime,
        exit_code: int | None,
        resource_usage: dict[str, Any] | None = None,
    ) -> ExecutionRunRecord:
        record = await session.get(ExecutionRunRecord, run_id, with_for_update=True)
        if record is None:
            raise LookupError(f"Execution run {run_id} does not exist")
        record.status = status
        record.exit_code = exit_code
        record.finished_at = finished_at
        record.resource_usage = dict(resource_usage or {})
        await session.flush()
        return record
