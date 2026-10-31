"""LEDGER's planned verification task waits for the real test run instead of completing trivially.

The task ORCHESTR plans ("Verify independently") depends on the FORGE implementation task. FORGE
sends LEDGER a ``verification.request`` carrying that task's id, so the run it opens is recorded
against the FORGE task. The verification task is finished only once such a run has a verdict.
Task lookups go through LEDGER's work queue; only the test-run read is LEDGER's own table.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.work_queue import MAX_LIMIT, AgentWorkQueue, WorkItem
from aether.communication.messages import AgentId
from aether.storage.models import RunStatus, TaskStatus, TestRunRecord

_OPEN = (RunStatus.PENDING.value, RunStatus.RUNNING.value)


@dataclass(frozen=True, slots=True)
class VerificationProgress:
    """Whether the verification task has the independent test run it waits for."""

    forge_task_ids: tuple[UUID, ...]
    run: TestRunRecord | None

    @property
    def finished(self) -> bool:
        return self.run is not None


async def forge_dependencies(work: AgentWorkQueue, task_id: UUID) -> tuple[UUID, ...]:
    """The FORGE tasks ``task_id`` depends on, oldest first."""
    detail = await work.task_detail(task_id)
    if detail is None:
        return ()
    return tuple(
        item.task_id for item in detail.depends_on if item.assigned_agent == AgentId.FORGE.value
    )


async def verification_progress(
    sessions: async_sessionmaker[AsyncSession], work: AgentWorkQueue, task_id: UUID
) -> VerificationProgress | None:
    """None when the task is not a verification of FORGE work (it then completes as before)."""
    forge_ids = await forge_dependencies(work, task_id)
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


async def waiting_verification_tasks(work: AgentWorkQueue) -> list[WorkItem]:
    """LEDGER's own tasks parked until their independent test run has a verdict."""
    return await work.open_tasks(states={TaskStatus.WAITING_FOR_VERIFICATION}, limit=MAX_LIMIT)
