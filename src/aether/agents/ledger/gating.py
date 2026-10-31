"""LEDGER's planned verification task waits for the real test run instead of completing trivially.

The task ORCHESTR plans ("Verify independently") depends on the FORGE implementation task. FORGE
sends LEDGER a ``verification.request`` carrying that task's id, so the run it opens is recorded
against the FORGE task. The verification task is finished only once such a run has a verdict.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId
from aether.storage.models import (
    RunStatus,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
    TestRunRecord,
)

_OPEN = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


@dataclass(frozen=True, slots=True)
class VerificationProgress:
    """Whether the verification task has the independent test run it waits for."""

    forge_task_ids: tuple[UUID, ...]
    run: TestRunRecord | None

    @property
    def finished(self) -> bool:
        return self.run is not None


async def forge_dependencies(
    sessions: async_sessionmaker[AsyncSession], task_id: UUID
) -> tuple[UUID, ...]:
    statement = (
        select(TaskRecord.id)
        .join(TaskDependencyRecord, TaskDependencyRecord.depends_on_task_id == TaskRecord.id)
        .where(
            TaskDependencyRecord.task_id == task_id,
            TaskRecord.assigned_agent == AgentId.FORGE.value,
        )
        .order_by(TaskRecord.created_at, TaskRecord.id)
    )
    async with sessions() as session:
        return tuple(await session.scalars(statement))


async def verification_progress(
    sessions: async_sessionmaker[AsyncSession], task: TaskRecord
) -> VerificationProgress | None:
    """None when ``task`` is not a verification of FORGE work (it then completes as before)."""
    forge_ids = await forge_dependencies(sessions, task.id)
    if not forge_ids:
        return None
    statement = (
        select(TestRunRecord)
        .where(TestRunRecord.task_id.in_(forge_ids), TestRunRecord.status.not_in(_OPEN))
        .order_by(TestRunRecord.finished_at.desc(), TestRunRecord.started_at.desc())
        .limit(1)
    )
    async with sessions() as session:
        run = await session.scalar(statement)
    return VerificationProgress(forge_task_ids=forge_ids, run=run)


async def waiting_verification_tasks(
    sessions: async_sessionmaker[AsyncSession],
) -> list[TaskRecord]:
    statement = select(TaskRecord).where(
        TaskRecord.assigned_agent == AgentId.LEDGER.value,
        TaskRecord.state == TaskStatus.WAITING_FOR_APPROVAL.value,
    )
    async with sessions() as session:
        return list(await session.scalars(statement))
