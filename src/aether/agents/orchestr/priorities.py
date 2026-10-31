"""Applying the pure priority formula to stored objectives."""

from datetime import datetime
from uuid import UUID

from aether.agents.orchestr.env import Env, as_utc
from aether.agents.orchestr.priority import (
    Boost,
    PriorityBreakdown,
    PriorityInputs,
    compute_priority,
    parse_boost_reason,
    parse_set_reason,
)
from aether.agents.orchestr.store import ACTOR
from aether.storage.models import ObjectiveRecord

COST_SCALE = 100.0
_CHILD_STATUSES = ("active", "paused", "blocked", "deferred")
_CRITICAL_CHILDREN = 3


def _hours(now: datetime, then: datetime) -> float:
    return max(0.0, (now - as_utc(then)).total_seconds() / 3600)


async def priority_inputs(env: Env, objective: ObjectiveRecord) -> PriorityInputs:
    """Gather base, boosts and context from the database; time comes from ``env.now()``."""
    now = env.now()
    base = objective.base_priority
    last_reset = as_utc(objective.created_at)
    boosts: list[Boost] = []
    for created_at, _actor, payload in await env.store.objective_events(
        objective.id, "objective.priority_changed"
    ):
        reason = str(payload.get("reason", ""))
        when = as_utc(created_at)
        if (setting := parse_set_reason(reason)) is not None:
            base, last_reset = setting[0], max(last_reset, when)
            boosts.clear()
        elif (boost := parse_boost_reason(reason)) is not None:
            boosts.append(Boost(points=boost[0], age_hours=_hours(now, when)))
            last_reset = max(last_reset, when)

    tasks = await env.store.tasks_of(objective.id)
    completed = [as_utc(task.completed_at) for task in tasks if task.completed_at is not None]
    waiting_since = max([as_utc(objective.created_at), *completed])
    hours_to_deadline = (
        (as_utc(objective.deadline) - now).total_seconds() / 3600
        if objective.deadline is not None
        else None
    )
    children = await env.store.count_children(objective.id, _CHILD_STATUSES)
    return PriorityInputs(
        base_priority=base,
        hours_since_boost=_hours(now, last_reset),
        hours_waiting=_hours(now, waiting_since),
        hours_to_deadline=hours_to_deadline,
        dependency_criticality=min(1.0, children / _CRITICAL_CHILDREN),
        resource_cost=(objective.estimated_cost or 0.0) / COST_SCALE,
        expected_benefit=0.5 if objective.expected_benefit else 0.0,
        boosts=tuple(boosts),
    )


async def objective_breakdown(env: Env, objective: ObjectiveRecord) -> PriorityBreakdown:
    return compute_priority(await priority_inputs(env, objective), env.limits.priority)


async def refresh_priority(env: Env, objective: ObjectiveRecord) -> int:
    """Recompute the effective priority; persist only meaningful changes. Returns the value."""
    breakdown = await objective_breakdown(env, objective)
    if abs(breakdown.effective - objective.priority) >= env.limits.priority.min_change_to_persist:
        await env.store.objectives.update_priority(
            objective.id,
            breakdown.effective,
            actor=ACTOR,
            reason=(
                f"recomputed: base {breakdown.decayed_base:.1f}, urgency {breakdown.urgency:.1f},"
                f" boost {breakdown.boost:.1f}"
            ),
        )
        return breakdown.effective
    return objective.priority


async def refresh_all(env: Env, objectives: list[ObjectiveRecord]) -> dict[UUID, int]:
    return {objective.id: await refresh_priority(env, objective) for objective in objectives}


def task_rank(task_priority: int, objective: ObjectiveRecord) -> int:
    """A task ranks by its own offset plus however far its objective has moved from its start."""
    return max(0, min(100, task_priority + objective.priority - objective.base_priority))
