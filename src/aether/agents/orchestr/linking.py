"""Requirement linking: ORCHESTR binds approved requirements to the tasks that build and verify them.

FORGE and LEDGER must never guess which requirement a task serves. ORCHESTR observes SCOUT's
consolidation / evaluation and records the link on the task through the repository (which emits
``task.requirement_linked``). Everything here is idempotent, so redelivery changes nothing.
"""

from uuid import UUID

from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planner import StepKind, kind_of_idempotency_key
from aether.agents.orchestr.store import ACTOR
from aether.communication.contracts import (
    EvaluationOutcome,
    Payload,
    RequirementEvaluatedPayload,
    ResearchConsolidatedPayload,
)
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import RequirementStatus, TaskRecord, TaskStatus

LINKED_AGENTS = frozenset({AgentId.FORGE.value, AgentId.LEDGER.value})
_UNLINKED_REQUIREMENTS = frozenset(
    {RequirementStatus.REJECTED.value, RequirementStatus.SUPERSEDED.value}
)
# Requirement states that do not (or no longer) authorize implementation work.
_NOT_APPROVED = _UNLINKED_REQUIREMENTS | {
    RequirementStatus.PROPOSED.value,
    RequirementStatus.EVALUATING.value,
    RequirementStatus.NEEDS_EVIDENCE.value,
}
_FINISHED = frozenset({TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value})


def needs_requirement(task: TaskRecord) -> bool:
    return task.assigned_agent in LINKED_AGENTS and str(task.state) not in _FINISHED


async def link_requirement(
    env: Env,
    objective_id: UUID,
    requirement_id: UUID,
    *,
    reason: str,
    correlation_id: UUID | None = None,
) -> list[UUID]:
    """Link ``requirement_id`` to every unlinked FORGE/LEDGER task of the objective."""
    linked: list[UUID] = []
    for task in await env.store.tasks_of(objective_id):
        if not needs_requirement(task) or task.requirement_id is not None:
            continue
        try:
            changed = await env.store.tasks.set_requirement(
                task.id,
                requirement_id,
                actor=ACTOR,
                reason=reason,
                correlation_id=correlation_id,
            )
        except ValueError:
            env.logger.warning("task %s is already linked to another requirement", task.id)
            continue
        if changed:
            linked.append(task.id)
    return linked


async def objective_requirement(env: Env, objective_id: UUID) -> UUID | None:
    """The requirement already linked to the objective's tasks, for tasks planned later."""
    for task in await env.store.tasks_of(objective_id):
        if task.requirement_id is not None and task.assigned_agent in LINKED_AGENTS:
            return task.requirement_id
    return None


async def handle_consolidated(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, ResearchConsolidatedPayload)
    objective_id = payload.objective_id or message.objective_id
    if objective_id is None:
        return
    for requirement_id in payload.requirement_ids[:1]:
        requirement = await env.store.get_requirement(requirement_id)
        if (
            requirement is None
            or requirement.objective_id != objective_id
            or str(requirement.status) in _UNLINKED_REQUIREMENTS
        ):
            continue
        await link_requirement(
            env,
            objective_id,
            requirement_id,
            reason="research.consolidated adopted the requirement",
            correlation_id=message.correlation_id,
        )


async def handle_evaluated(env: Env, message: AgentMessage, payload: Payload) -> None:
    assert isinstance(payload, RequirementEvaluatedPayload)
    if payload.outcome is not EvaluationOutcome.APPROVED:
        return
    await link_requirement(
        env,
        payload.objective_id,
        payload.requirement_id,
        reason="requirement.evaluated approved the requirement",
        correlation_id=message.correlation_id,
    )


async def is_ready_for_forge(env: Env, task: TaskRecord) -> bool:
    """FORGE work that depends on a requirement runs only once it is linked and approved."""
    if task.requirement_id is None:
        return False
    requirement = await env.store.get_requirement(task.requirement_id)
    return requirement is not None and str(requirement.status) not in _NOT_APPROVED


async def held_for_requirement(env: Env, tasks: list[TaskRecord]) -> set[UUID]:
    """Ids of FORGE tasks that must wait for an approved, linked requirement.

    Only objectives whose plan researches and consolidates produce requirements; implementation
    objectives without a consolidation step have nothing to wait for.
    """
    held: set[UUID] = set()
    researched: dict[UUID, bool] = {}
    for task in tasks:
        if task.assigned_agent != AgentId.FORGE.value:
            continue
        if task.objective_id not in researched:
            researched[task.objective_id] = any(
                kind_of_idempotency_key(item.idempotency_key) is StepKind.CONSOLIDATION
                for item in await env.store.tasks_of(task.objective_id)
            )
        if researched[task.objective_id] and not await is_ready_for_forge(env, task):
            held.add(task.id)
    return held
