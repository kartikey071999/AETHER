"""Dynamic objective priority: a pure formula with time decay and evidence boosts.

``compute_priority`` has no clock, database or randomness; callers pass durations. Decay lowers
the *base* component only and never below ``retention_floor``, so an objective can fade in the
queue but is never deleted, completed or starved to zero by time alone. Boosts (new evidence, a
nearer deadline, a blocker on the critical path) restart the decay clock and add points that
themselves fade with ``boost_half_life_hours``.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

BOOST_PREFIX = "boost"
SET_PREFIX = "set"
_BOOST_PATTERN = re.compile(r"^boost:(\d+(?:\.\d+)?):(.*)$", re.DOTALL)
_SET_PATTERN = re.compile(r"^set:(\d{1,3}):(.*)$", re.DOTALL)


class PriorityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    decay_half_life_hours: float = Field(default=72.0, gt=0)
    retention_floor: float = Field(default=0.25, ge=0.0, le=1.0)
    aging_points_per_hour: float = Field(default=0.05, ge=0.0)
    max_aging_points: float = Field(default=8.0, ge=0.0)
    max_urgency_points: float = Field(default=30.0, ge=0.0)
    urgency_horizon_hours: float = Field(default=72.0, gt=0)
    max_criticality_points: float = Field(default=10.0, ge=0.0)
    max_benefit_points: float = Field(default=8.0, ge=0.0)
    max_cost_penalty: float = Field(default=10.0, ge=0.0)
    boost_half_life_hours: float = Field(default=24.0, gt=0)
    max_boost_points: float = Field(default=40.0, ge=0.0)
    min_effective_priority: int = Field(default=1, ge=0, le=100)
    min_change_to_persist: int = Field(default=2, ge=1)


@dataclass(frozen=True, slots=True)
class Boost:
    points: float
    age_hours: float


@dataclass(frozen=True, slots=True)
class PriorityInputs:
    base_priority: int
    hours_since_boost: float
    hours_waiting: float = 0.0
    hours_to_deadline: float | None = None
    dependency_criticality: float = 0.0
    resource_cost: float = 0.0
    expected_benefit: float = 0.0
    boosts: Sequence[Boost] = ()


@dataclass(frozen=True, slots=True)
class PriorityBreakdown:
    base: float
    decayed_base: float
    aging: float
    urgency: float
    criticality: float
    benefit: float
    cost_penalty: float
    boost: float
    total: float
    effective: int


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def retention(hours_since_boost: float, policy: PriorityPolicy) -> float:
    """Share of the base priority that remains; non-increasing in time, never below the floor."""
    elapsed = max(0.0, hours_since_boost)
    return max(policy.retention_floor, 0.5 ** (elapsed / policy.decay_half_life_hours))


def boost_contribution(boosts: Sequence[Boost], policy: PriorityPolicy) -> float:
    total = sum(
        max(0.0, boost.points) * 0.5 ** (max(0.0, boost.age_hours) / policy.boost_half_life_hours)
        for boost in boosts
    )
    return min(total, policy.max_boost_points)


def urgency_points(hours_to_deadline: float | None, policy: PriorityPolicy) -> float:
    if hours_to_deadline is None:
        return 0.0
    if hours_to_deadline <= 0:
        return policy.max_urgency_points
    remaining = _unit(1.0 - hours_to_deadline / policy.urgency_horizon_hours)
    return policy.max_urgency_points * remaining


def compute_priority(inputs: PriorityInputs, policy: PriorityPolicy) -> PriorityBreakdown:
    base = float(min(100, max(0, inputs.base_priority)))
    decayed_base = base * retention(inputs.hours_since_boost, policy)
    aging = min(
        max(0.0, inputs.hours_waiting) * policy.aging_points_per_hour, policy.max_aging_points
    )
    urgency = urgency_points(inputs.hours_to_deadline, policy)
    criticality = _unit(inputs.dependency_criticality) * policy.max_criticality_points
    benefit = _unit(inputs.expected_benefit) * policy.max_benefit_points
    cost_penalty = _unit(inputs.resource_cost) * policy.max_cost_penalty
    boost = boost_contribution(inputs.boosts, policy)
    total = decayed_base + aging + urgency + criticality + benefit + boost - cost_penalty
    # Time alone never pushes a live objective to zero; an explicit base of 0 stays 0.
    floor = policy.min_effective_priority if base > 0 else 0
    effective = min(100, max(floor, round(total)))
    return PriorityBreakdown(
        base=base,
        decayed_base=decayed_base,
        aging=aging,
        urgency=urgency,
        criticality=criticality,
        benefit=benefit,
        cost_penalty=cost_penalty,
        boost=boost,
        total=total,
        effective=effective,
    )


def format_boost_reason(points: float, why: str) -> str:
    return f"{BOOST_PREFIX}:{max(0.0, points):g}:{why}"


def parse_boost_reason(reason: str) -> tuple[float, str] | None:
    match = _BOOST_PATTERN.match(reason)
    return (float(match.group(1)), match.group(2)) if match else None


def format_set_reason(priority: int, why: str) -> str:
    return f"{SET_PREFIX}:{min(100, max(0, priority))}:{why}"


def parse_set_reason(reason: str) -> tuple[int, str] | None:
    match = _SET_PATTERN.match(reason)
    if match is None or int(match.group(1)) > 100:
        return None
    return int(match.group(1)), match.group(2)
