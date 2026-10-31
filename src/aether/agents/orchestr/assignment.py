"""Releasing tasks to the runtime and telling the assigned agent what to do."""

from aether.agents.orchestr.env import Env, as_utc
from aether.agents.orchestr.planner import StepKind, kind_of_idempotency_key
from aether.agents.orchestr.planning import parse_acceptance
from aether.agents.orchestr.store import ACTOR, stable_id
from aether.communication.contracts import (
    ResearchBudget,
    ResearchMode,
    ResearchRequestPayload,
    ResourceEstimate,
    TaskAssignPayload,
    build_message,
)
from aether.communication.messages import AgentId
from aether.storage.models import ObjectiveRecord, TaskRecord, TaskStatus
from aether.storage.repositories import InvalidTaskTransition

_RESEARCH_BUDGET = {
    AgentId.SCOUT: ResearchBudget(max_llm_requests=20, max_sources=10),
    AgentId.ROGUE: ResearchBudget(max_llm_requests=10, max_sources=10),
}


async def publish_assignment(env: Env, task: TaskRecord, objective: ObjectiveRecord) -> None:
    """Send ``task.assign`` (and ``research.request`` for research steps) to the assignee.

    Message ids derive from task and attempt, so republishing after a restart is a no-op.
    ORCHESTR's own coordination tasks run in-process and are never messaged.
    """
    if task.assigned_agent is None:
        return
    agent = AgentId(task.assigned_agent)
    if agent is AgentId.ORCHESTR:
        return
    dependencies = sorted((await env.store.dependencies_of([task.id]))[task.id], key=str)
    payload = TaskAssignPayload(
        task_id=task.id,
        objective_id=task.objective_id,
        title=task.title,
        description=task.description,
        assigned_agent=agent,
        acceptance_criteria=parse_acceptance(task.description),
        depends_on=dependencies,
        priority=task.priority,
        resource_estimate=ResourceEstimate(
            ram_mb=task.estimated_ram_mb,
            disk_mb=task.estimated_disk_mb,
            cpu_percent=task.estimated_cpu_percent or 0.0,
        ),
        interruptible=task.interruptible,
        deadline=as_utc(task.deadline) if task.deadline else None,
    )
    await env.publish(
        build_message(
            AgentId.ORCHESTR,
            agent,
            payload,
            message_id=stable_id("assign", task.id, task.attempt),
            objective_id=task.objective_id,
            task_id=task.id,
            priority=task.priority,
            requires_response=True,
        )
    )
    if (
        kind_of_idempotency_key(task.idempotency_key) is StepKind.RESEARCH
        and agent in _RESEARCH_BUDGET
    ):
        research = ResearchRequestPayload(
            objective_id=task.objective_id,
            question=task.title,
            scope=objective.title,
            budget=_RESEARCH_BUDGET[agent],
            mode=ResearchMode.UNCONVENTIONAL
            if agent is AgentId.ROGUE
            else ResearchMode.CONVENTIONAL,
            task_id=task.id,
        )
        await env.publish(
            build_message(
                AgentId.ORCHESTR,
                agent,
                research,
                message_id=stable_id("research", task.id, task.attempt),
                objective_id=task.objective_id,
                task_id=task.id,
                priority=task.priority,
            )
        )


async def release_task(env: Env, task: TaskRecord, objective: ObjectiveRecord) -> bool:
    """PENDING -> READY, then assign. Returns False if the task already moved on."""
    try:
        await env.store.tasks.transition(
            task.id,
            TaskStatus.READY,
            actor=ACTOR,
            details={"reason": "dependencies complete; scheduler selected"},
        )
    except InvalidTaskTransition:
        return False
    refreshed = await env.store.get_task(task.id)
    await publish_assignment(env, refreshed or task, objective)
    return True
