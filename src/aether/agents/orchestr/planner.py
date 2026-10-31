"""Deterministic objective decomposition: templates per objective kind, validated as a DAG.

The LLM may only *refine* a template (``apply_refinement``); every plan, refined or not, must pass
``validate_plan`` (unique keys, known dependencies, no cycles, bounded size and depth).
"""

import re
from collections import deque
from collections.abc import Iterable, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.orchestr.schemas import KEY_PATTERN, PlanRefinement
from aether.communication.messages import AgentId


class PlanError(ValueError):
    """A plan violates structure or limits and must not be persisted."""


class ObjectiveKind(StrEnum):
    CAPABILITY = "capability"
    RESEARCH = "research"
    IMPLEMENTATION = "implementation"


class StepKind(StrEnum):
    RESEARCH = "research"
    CONSOLIDATION = "consolidation"
    SECURITY_REVIEW = "security_review"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REWORK = "rework"
    REVERIFY = "reverify"
    BLOCKER = "blocker"
    EXTRA = "extra"
    TASK = "task"


class TaskSpec(BaseModel):
    """One planned task. ``key`` is unique within the plan and prefixed by its step kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    agent: AgentId
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    priority_offset: int = Field(default=0, ge=-50, le=50)
    ram_mb: int = Field(default=0, ge=0)
    disk_mb: int = Field(default=0, ge=0)
    cpu_percent: float = Field(default=10.0, ge=0.0, le=100.0)
    duration_s: int | None = Field(default=None, ge=0)
    interruptible: bool = True

    @property
    def kind(self) -> StepKind:
        return step_kind(self.key)


class PlanDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ObjectiveKind
    tasks: tuple[TaskSpec, ...]
    source: str = "template"


_PLAN_KEY_PREFIX = "plan:"
_BLOCKER_KEY_PREFIX = "blocker:"

DEFAULT_CRITERIA: dict[ObjectiveKind, tuple[str, ...]] = {
    ObjectiveKind.CAPABILITY: (
        "The capability is implemented and verified against its acceptance criteria",
    ),
    ObjectiveKind.RESEARCH: ("Findings are consolidated with a recommendation and evidence",),
    ObjectiveKind.IMPLEMENTATION: (
        "The change is implemented and verified against its acceptance criteria",
    ),
}

# CPU is a share of ONE core (the scheduler compares it with idle core capacity, not host percent).
_RESOURCES: dict[StepKind, dict[str, int | float | bool | None]] = {
    StepKind.RESEARCH: {"ram_mb": 256, "disk_mb": 50, "cpu_percent": 15.0, "duration_s": 900},
    StepKind.CONSOLIDATION: {"ram_mb": 128, "disk_mb": 10, "cpu_percent": 10.0, "duration_s": 300},
    StepKind.SECURITY_REVIEW: {"ram_mb": 64, "disk_mb": 5, "cpu_percent": 5.0, "duration_s": 300},
    StepKind.IMPLEMENTATION: {
        "ram_mb": 2048,
        "disk_mb": 2048,
        "cpu_percent": 60.0,
        "duration_s": 3600,
        "interruptible": False,
    },
    StepKind.VERIFICATION: {"ram_mb": 1024, "disk_mb": 256, "cpu_percent": 40.0, "duration_s": 900},
}

_CAPABILITY_WORDS = re.compile(r"\b(capabilit(?:y|ies)|abilit(?:y|ies)|able to|features?)\b")
_IMPLEMENTATION_WORDS = re.compile(
    r"\b(implement|build|install|integrate|fix|deploy|add|create|refactor|migrate)\b"
)
_RESEARCH_WORDS = re.compile(
    r"\b(research|investigate|evaluate|compare|explore|survey|study|analy[sz]e|assess)\b"
)


def step_kind(key: str) -> StepKind:
    prefix = key.split(".", 1)[0]
    try:
        return StepKind(prefix)
    except ValueError:
        return StepKind.TASK


def plan_idempotency_key(objective_id: object, key: str) -> str:
    return f"{_PLAN_KEY_PREFIX}{objective_id}:{key}"


def blocker_idempotency_key(blocker_id: object, kind: str) -> str:
    return f"{_BLOCKER_KEY_PREFIX}{blocker_id}:{kind}"


def kind_of_idempotency_key(idempotency_key: str | None) -> StepKind:
    """The step kind recorded in a task's idempotency key (planned, blocker or other)."""
    if not idempotency_key:
        return StepKind.TASK
    if idempotency_key.startswith(_PLAN_KEY_PREFIX):
        return step_kind(idempotency_key.split(":", 2)[2])
    if idempotency_key.startswith(_BLOCKER_KEY_PREFIX):
        return StepKind.BLOCKER
    return StepKind.TASK


def classify_objective(title: str, description: str = "") -> ObjectiveKind:
    """Keyword classification; unknown phrasing defaults to research, which has no side effects."""
    text = f"{title}\n{description}".lower()
    if _CAPABILITY_WORDS.search(text):
        return ObjectiveKind.CAPABILITY
    if _IMPLEMENTATION_WORDS.search(text):
        return ObjectiveKind.IMPLEMENTATION
    if _RESEARCH_WORDS.search(text):
        return ObjectiveKind.RESEARCH
    return ObjectiveKind.RESEARCH


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _spec(key: str, title: str, agent: AgentId, **fields: object) -> TaskSpec:
    estimate = {
        name: value
        for name, value in _RESOURCES.get(step_kind(key), {}).items()
        if value is not None
    }
    return TaskSpec(key=key, title=title[:200], agent=agent, **{**estimate, **fields})  # type: ignore[arg-type]


def build_plan(
    kind: ObjectiveKind,
    title: str,
    description: str = "",
    completion_criteria: Sequence[str] = (),
) -> list[TaskSpec]:
    """The deterministic template for ``kind``; ``completion_criteria`` drive verification.

    There is no communication step: ORCHESTR hands the final outcome to MUSE itself on completion.
    """
    subject = _short(title)
    criteria = tuple(completion_criteria) or DEFAULT_CRITERIA[kind]
    research_scout = _spec(
        "research.scout",
        f"Research conventional approaches: {subject}",
        AgentId.SCOUT,
        description=f"Gather evidence on mainstream approaches for: {subject}",
        acceptance_criteria=("Findings submitted with evidence and a classification",),
        priority_offset=5,
    )
    research_rogue = _spec(
        "research.rogue",
        f"Explore unconventional approaches: {subject}",
        AgentId.ROGUE,
        description=f"Explore unconventional approaches for: {subject}",
        acceptance_criteria=("Findings submitted to SCOUT with evidence and a classification",),
    )
    consolidation = _spec(
        "consolidation",
        f"Consolidate findings and requirements: {subject}",
        AgentId.SCOUT,
        depends_on=("research.scout", "research.rogue"),
        acceptance_criteria=(
            "Findings adopted or rejected with rationale; requirements submitted",
        ),
        priority_offset=5,
    )
    security = _spec(
        "security_review",
        f"Security review: {subject}",
        AgentId.WARDEN,
        depends_on=("consolidation",) if kind is ObjectiveKind.CAPABILITY else (),
        acceptance_criteria=("A scoped security decision is recorded before any execution",),
        priority_offset=10,
    )
    implementation = _spec(
        "implementation",
        f"Implement: {subject}",
        AgentId.FORGE,
        depends_on=("security_review",),
        acceptance_criteria=("Implementation matches the approved plan and requirements",),
    )
    verification = _spec(
        "verification",
        f"Verify independently: {subject}",
        AgentId.LEDGER,
        depends_on=("implementation",)
        if kind is not ObjectiveKind.RESEARCH
        else ("consolidation",),
        acceptance_criteria=criteria,
    )
    if kind is ObjectiveKind.RESEARCH:
        return [research_scout, research_rogue, consolidation, verification]
    if kind is ObjectiveKind.IMPLEMENTATION:
        return [security, implementation, verification]
    return [
        research_scout,
        research_rogue,
        consolidation,
        security,
        implementation,
        verification,
    ]


def topological_order(specs: Sequence[TaskSpec]) -> list[TaskSpec]:
    """Dependency order (dependencies first); raises ``PlanError`` on unknown keys or cycles."""
    by_key = {spec.key: spec for spec in specs}
    if len(by_key) != len(specs):
        raise PlanError("plan contains duplicate task keys")
    indegree = {spec.key: 0 for spec in specs}
    dependents: dict[str, list[str]] = {spec.key: [] for spec in specs}
    for spec in specs:
        for dependency in set(spec.depends_on):
            if dependency not in by_key:
                raise PlanError(f"task {spec.key!r} depends on unknown task {dependency!r}")
            if dependency == spec.key:
                raise PlanError(f"task {spec.key!r} depends on itself")
            indegree[spec.key] += 1
            dependents[dependency].append(spec.key)
    queue = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    ordered: list[TaskSpec] = []
    while queue:
        key = queue.popleft()
        ordered.append(by_key[key])
        for dependent in sorted(dependents[key]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(ordered) != len(specs):
        raise PlanError("plan dependencies contain a cycle")
    return ordered


def plan_depth(specs: Sequence[TaskSpec]) -> int:
    """Length (in tasks) of the longest dependency chain."""
    depth: dict[str, int] = {}
    for spec in topological_order(specs):
        depth[spec.key] = 1 + max((depth[key] for key in spec.depends_on), default=0)
    return max(depth.values(), default=0)


def validate_plan(specs: Sequence[TaskSpec], limits: OrchestrLimits) -> list[TaskSpec]:
    """Return the plan in dependency order or raise ``PlanError``."""
    if not specs:
        raise PlanError("a plan needs at least one task")
    if len(specs) > limits.max_tasks_per_objective:
        raise PlanError(f"plan has {len(specs)} tasks; limit is {limits.max_tasks_per_objective}")
    ordered = topological_order(specs)
    if plan_depth(specs) > limits.max_plan_depth:
        raise PlanError(f"plan dependency chain exceeds depth {limits.max_plan_depth}")
    return ordered


_EXTRA_AGENTS = frozenset({AgentId.SCOUT, AgentId.ROGUE, AgentId.FORGE, AgentId.LEDGER})
_RESEARCH_AGENTS = frozenset({AgentId.SCOUT, AgentId.ROGUE})


def _resolve(key: str, known: Iterable[str]) -> str | None:
    known = set(known)
    for candidate in (key, f"extra.{key}"):
        if candidate in known:
            return candidate
    return None


def apply_refinement(
    base: Sequence[TaskSpec], refinement: PlanRefinement, limits: OrchestrLimits
) -> list[TaskSpec]:
    """Merge model-proposed extra tasks into ``base``; raises ``PlanError`` if anything is unsafe.

    Extras may only be assigned to research/implementation/verification agents. FORGE extras
    always wait for the plan's security review, so the model cannot route around WARDEN.
    """
    if len(refinement.extra_tasks) > limits.max_refinement_tasks:
        raise PlanError("refinement proposes too many extra tasks")
    specs = {spec.key: spec for spec in base}
    order = list(specs)
    security_key = next((key for key in order if step_kind(key) is StepKind.SECURITY_REVIEW), None)
    extras: list[TaskSpec] = []
    for extra in refinement.extra_tasks:
        if extra.agent not in _EXTRA_AGENTS:
            raise PlanError(f"extra tasks may not be assigned to {extra.agent.value}")
        if not re.match(KEY_PATTERN, extra.key):
            raise PlanError("extra task key is not a valid slug")
        key = f"extra.{extra.key}"
        if key in specs:
            raise PlanError(f"duplicate extra task key {extra.key!r}")
        depends: list[str] = []
        for name in extra.depends_on:
            resolved = _resolve(name, specs)
            if resolved is None:
                raise PlanError(f"extra task {extra.key!r} depends on unknown task {name!r}")
            depends.append(resolved)
        if extra.agent is AgentId.FORGE:
            if security_key is None:
                raise PlanError("implementation extras require a security review in the plan")
            if security_key not in depends:
                depends.append(security_key)
        specs[key] = TaskSpec(
            key=key,
            title=extra.title,
            description=extra.description,
            agent=extra.agent,
            depends_on=tuple(depends),
            acceptance_criteria=tuple(extra.acceptance_criteria),
            cpu_percent=30.0 if extra.agent is AgentId.FORGE else 10.0,
            ram_mb=1024 if extra.agent is AgentId.FORGE else 128,
        )
        extras.append(specs[key])
    for extra, spec in zip(refinement.extra_tasks, extras, strict=True):
        default = "consolidation" if spec.agent in _RESEARCH_AGENTS else "verification"
        target = _resolve(extra.before, specs) if extra.before else None
        if extra.before and target is None:
            raise PlanError(f"extra task {extra.key!r} targets unknown task {extra.before!r}")
        target = target or next(
            (key for key in (default, "verification") if key in specs and key != spec.key), None
        )
        if target is None:
            continue
        gated = specs[target]
        if step_kind(target) is StepKind.SECURITY_REVIEW and spec.agent is AgentId.FORGE:
            raise PlanError("implementation extras cannot gate the security review")
        if spec.key not in gated.depends_on:
            specs[target] = gated.model_copy(update={"depends_on": (*gated.depends_on, spec.key)})
    merged = list(specs.values())
    validate_plan(merged, limits)
    return merged


def parse_kind(value: str | None) -> ObjectiveKind | None:
    try:
        return ObjectiveKind(value) if value else None
    except ValueError:
        return None
