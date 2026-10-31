"""Objective intake: validation, limits, creation, and the decision to plan or defer."""

from datetime import timedelta
from uuid import UUID

from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planner import DEFAULT_CRITERIA, classify_objective
from aether.agents.orchestr.planning import plan_objective
from aether.agents.orchestr.store import ACTOR
from aether.communication.contracts import ObjectiveCreatePayload
from aether.communication.messages import AgentId, AgentMessage

OPEN_STATUSES = ("active",)
LIVE_STATUSES = ("active", "paused", "blocked", "deferred")
LIMIT_DEFERRAL_REASON = "active objective limit reached"

# Everyone but the user's own channel (MUSE) and ORCHESTR is an autonomous proposer.
_AUTONOMOUS_SOURCES = frozenset(
    {AgentId.SCOUT, AgentId.ROGUE, AgentId.FORGE, AgentId.LEDGER, AgentId.WARDEN}
)


class ObjectiveRejected(ValueError):
    """The objective violates a limit or a bounding rule; the reason is recorded, not retried."""


def is_generated(source: AgentId, payload: ObjectiveCreatePayload) -> bool:
    return payload.generated or source in _AUTONOMOUS_SOURCES


async def _reject(env: Env, message: AgentMessage, reason: str) -> ObjectiveRejected:
    await env.store.record_decision(
        "objective_rejected",
        reason,
        key=f"orchestr:rejected:{message.message_id}",
        evidence=[message.source_agent.value, message.message_type.value],
        correlation_id=message.correlation_id,
    )
    return ObjectiveRejected(reason)


async def create_objective(
    env: Env, message: AgentMessage, payload: ObjectiveCreatePayload
) -> UUID:
    """Accept an objective; returns its id. Redelivery returns the already created objective."""
    existing = await env.store.find_created_objective(message.correlation_id, payload.title)
    if existing is not None:
        if existing.status == "active":
            await plan_objective(env, existing)
        return existing.id

    limits = env.limits
    source = message.source_agent
    generated = is_generated(source, payload)
    now = env.now()
    priority = payload.priority

    if generated:
        missing = [
            name
            for name, value in (
                ("rationale", payload.rationale),
                ("stopping_conditions", payload.stopping_conditions),
                ("completion_criteria", payload.completion_criteria),
                ("expected_benefit", payload.expected_benefit),
                ("estimated_cost", payload.estimated_cost),
            )
            if value is None or value == [] or value == ""
        ]
        if missing:
            raise await _reject(env, message, f"generated objectives require: {', '.join(missing)}")
        window = now - timedelta(hours=limits.generated_interval_hours)
        if (
            await env.store.count_generated_since(window)
            >= limits.max_generated_objectives_per_interval
        ):
            raise await _reject(env, message, "generated objective rate limit reached")
        priority = min(priority, limits.max_generated_priority)

    if (
        payload.parent_objective_id is not None
        and await env.store.get_objective(payload.parent_objective_id) is None
    ):
        raise await _reject(env, message, "parent objective does not exist")

    active = await env.store.count_objectives(OPEN_STATUSES)
    deferred = active >= limits.max_active_objectives
    if deferred and generated:
        raise await _reject(env, message, "active objective limit reached")

    kind = classify_objective(payload.title, payload.description)
    criteria = list(payload.completion_criteria) or list(DEFAULT_CRITERIA[kind])
    objective_id = await env.store.objectives.create(
        title=payload.title,
        description=payload.description,
        priority=priority,
        created_by=source.value,
        rationale=payload.rationale,
        completion_criteria=criteria,
        parent_objective_id=payload.parent_objective_id,
        deadline=payload.deadline,
        review_at=(
            now + timedelta(hours=limits.limit_deferral_hours) if deferred else payload.review_at
        ),
        expected_benefit=payload.expected_benefit,
        estimated_cost=payload.estimated_cost,
        stopping_conditions=list(payload.stopping_conditions),
        generated_by_agent=generated,
        correlation_id=message.correlation_id,
    )
    if deferred:
        await env.store.objectives.set_status(
            objective_id,
            "deferred",
            actor=ACTOR,
            reason=LIMIT_DEFERRAL_REASON,
            correlation_id=message.correlation_id,
        )
    await env.store.record_decision(
        "objective_accepted",
        "deferred: active objective limit reached" if deferred else "accepted and planned",
        key=f"orchestr:intake:{objective_id}",
        evidence=[source.value, "generated" if generated else "user"],
        objective_id=objective_id,
        correlation_id=message.correlation_id,
    )
    if not deferred:
        objective = await env.store.get_objective(objective_id)
        if objective is not None:
            await plan_objective(env, objective)
    return objective_id
