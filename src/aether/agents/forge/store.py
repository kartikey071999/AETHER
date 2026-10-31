"""Durable FORGE state kept in the message log: plan lookup and the per-step journal.

FORGE owns no table. Plans are `execution.request` / `implementation.plan` messages, and step
state is a series of FORGE `progress.update` messages with deterministic ids, so a restart (or a
redelivered message) resumes exactly where recorded state says execution stopped.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aether.agents.base import AgentContext
from aether.agents.forge.planning import forge_id
from aether.agents.forge.schemas import ForgePlan
from aether.communication.contracts import (
    SENDERS,
    ExecutionRequestPayload,
    ImplementationPlanPayload,
    ProgressUpdatePayload,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.storage.models import MessageRecord, TaskRecord

JOURNAL_KEY = "forge_journal"
_PLAN_TYPES = (MessageType.EXECUTION_REQUEST.value, MessageType.IMPLEMENTATION_PLAN.value)


def plan_from_message(record: "MessageRecord | AgentMessage") -> ForgePlan | None:
    """A plan from a stored message, or ``None`` when it is invalid or from a disallowed sender."""
    kind = MessageType(record.message_type)
    if AgentId(record.source_agent) not in SENDERS.get(kind, frozenset()):
        return None
    try:
        if kind is MessageType.EXECUTION_REQUEST:
            request = ExecutionRequestPayload.model_validate(record.payload)
            return ForgePlan(
                plan_id=request.plan_id,
                task_id=request.task_id,
                objective_id=request.objective_id,
                requirement_id=request.requirement_id,
                steps=tuple(request.steps),
                rollback_steps=tuple(request.rollback_steps),
                acceptance_criteria=tuple(request.acceptance_criteria),
                resource_estimate=request.resource_estimate,
                depends_on=tuple(request.depends_on),
                source="execution.request",
            )
        plan = ImplementationPlanPayload.model_validate(record.payload)
    except ValueError:
        return None
    task_id = plan.task_id or record.task_id
    if task_id is None:
        return None
    return ForgePlan(
        plan_id=plan.plan_id,
        task_id=task_id,
        objective_id=record.objective_id,
        requirement_id=plan.requirement_id,
        steps=tuple(plan.steps),
        rollback_steps=tuple(plan.rollback_steps),
        acceptance_criteria=tuple(plan.acceptance_criteria),
        resource_estimate=plan.resource_estimate,
        source="implementation.plan",
    )


async def latest_plan(ctx: AgentContext, task_id: UUID) -> ForgePlan | None:
    """The newest valid plan addressed to FORGE (or stored by FORGE) for this task."""
    statement = (
        select(MessageRecord)
        .where(
            MessageRecord.task_id == task_id,
            MessageRecord.message_type.in_(_PLAN_TYPES),
            (MessageRecord.target_agent == AgentId.FORGE.value)
            | (MessageRecord.source_agent == AgentId.FORGE.value),
        )
        .order_by(MessageRecord.created_at.desc(), MessageRecord.id)
    )
    async with ctx.sessions() as session:
        records = list(await session.scalars(statement))
    for record in records:
        plan = plan_from_message(record)
        if plan is not None and plan.task_id == task_id:
            return plan
    return None


async def save_plan(ctx: AgentContext, task: TaskRecord, plan: ForgePlan) -> bool:
    """Persist a FORGE-built plan (idempotent); needs a requirement and acceptance criteria."""
    if plan.requirement_id is None or not plan.acceptance_criteria:
        return False
    payload = ImplementationPlanPayload(
        plan_id=plan.plan_id,
        requirement_id=plan.requirement_id,
        task_id=task.id,
        steps=list(plan.steps),
        rollback_steps=list(plan.rollback_steps),
        resource_estimate=plan.resource_estimate,
        acceptance_criteria=list(plan.acceptance_criteria),
    )
    message = build_message(
        AgentId.FORGE,
        AgentId.LEDGER,
        payload,
        message_id=forge_id("plan-message", task.id, plan.plan_id),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=plan.requirement_id,
    )
    return await ctx.bus.publish(message)


@dataclass(slots=True)
class Journal:
    """Ordered step records for one plan; the single source of truth for resume."""

    ctx: AgentContext
    task: TaskRecord
    plan_id: UUID
    entries: list[dict[str, Any]] = field(default_factory=list)

    async def load(self) -> "Journal":
        statement = select(MessageRecord).where(
            MessageRecord.task_id == self.task.id,
            MessageRecord.source_agent == AgentId.FORGE.value,
            MessageRecord.message_type == MessageType.PROGRESS_UPDATE.value,
        )
        async with self.ctx.sessions() as session:
            records = list(await session.scalars(statement))
        entries = []
        for record in records:
            details = (record.payload or {}).get("details") or {}
            if details.get(JOURNAL_KEY) and details.get("plan_id") == str(self.plan_id):
                entries.append(details)
        self.entries = sorted(entries, key=lambda item: int(item.get("seq", 0)))
        return self

    async def append(self, phase: str, index: int, status: str, **extra: Any) -> dict[str, Any]:
        for _ in range(5):
            seq = (self.entries[-1]["seq"] if self.entries else 0) + 1
            details = {
                JOURNAL_KEY: True,
                "plan_id": str(self.plan_id),
                "seq": seq,
                "phase": phase,
                "index": index,
                "status": status,
                **extra,
            }
            payload = ProgressUpdatePayload(
                task_id=self.task.id,
                objective_id=self.task.objective_id,
                summary=f"FORGE {phase}[{index}] {status}",
                details=details,
            )
            message = build_message(
                AgentId.FORGE,
                AgentId.ORCHESTR,
                payload,
                message_id=forge_id("journal", self.task.id, self.plan_id, seq),
                task_id=self.task.id,
                objective_id=self.task.objective_id,
                requirement_id=self.task.requirement_id,
            )
            if await self.ctx.bus.publish(message):
                self.entries.append(details)
                return details
            await self.load()
        raise RuntimeError("could not append to the FORGE step journal")

    def _matching(self, phase: str, index: int | None = None) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self.entries
            if entry["phase"] == phase and (index is None or entry["index"] == index)
        ]

    def succeeded(self, phase: str, index: int) -> bool:
        return any(entry["status"] == "succeeded" for entry in self._matching(phase, index))

    def failures(self, phase: str, index: int) -> int:
        return sum(1 for entry in self._matching(phase, index) if entry["status"] == "failed")

    def abandoned(self) -> dict[str, Any] | None:
        """The terminal step failure that sent execution to rollback, if one was recorded."""
        found = [e for e in self.entries if e["status"] == "abandoned"]
        return found[-1] if found else None

    def final(self) -> dict[str, Any] | None:
        found = self._matching("final")
        return found[-1] if found else None

    def evidence(self) -> list[dict[str, Any]]:
        """Step evidence in execution order: the last record per phase/index."""
        latest: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in self.entries:
            if entry["phase"] != "final" and isinstance(entry.get("evidence"), dict):
                latest[(entry["phase"], entry["index"])] = entry["evidence"]
        return list(latest.values())

    def run_ids(self) -> list[str]:
        seen: list[str] = []
        for item in self.evidence():
            run_id = item.get("run_id")
            if isinstance(run_id, str) and run_id not in seen:
                seen.append(run_id)
        return seen
