"""Plan creation: template, optional LLM refinement, persistence and idempotent materialization."""

from collections.abc import Sequence
from uuid import UUID

from aether.agents.orchestr.env import Env
from aether.agents.orchestr.linking import LINKED_AGENTS, objective_requirement
from aether.agents.orchestr.planner import (
    ObjectiveKind,
    PlanDocument,
    PlanError,
    TaskSpec,
    apply_refinement,
    build_plan,
    classify_objective,
    parse_kind,
    plan_idempotency_key,
    validate_plan,
)
from aether.agents.orchestr.prompts import PLAN_REFINEMENT_SYSTEM, plan_refinement_user
from aether.agents.orchestr.schemas import PlanRefinement
from aether.agents.orchestr.store import ACTOR
from aether.communication.messages import AgentId
from aether.models.contracts import CompletionMessage, CompletionRequest, ModelTier
from aether.models.facade import ModelCallContext
from aether.security.untrusted import UntrustedContent
from aether.storage.models import ObjectiveRecord

_ACCEPTANCE_HEADER = "Acceptance criteria:"
_MAX_PRIORITY = 100


def render_description(spec: TaskSpec) -> str:
    if not spec.acceptance_criteria:
        return spec.description
    lines = "\n".join(f"- {item}" for item in spec.acceptance_criteria)
    return f"{spec.description}\n\n{_ACCEPTANCE_HEADER}\n{lines}".strip()


def parse_acceptance(description: str) -> list[str]:
    """Acceptance criteria rendered by ``render_description``; empty for hand-made tasks."""
    if _ACCEPTANCE_HEADER not in description:
        return []
    tail = description.split(_ACCEPTANCE_HEADER, 1)[1]
    return [line[2:].strip() for line in tail.splitlines() if line.startswith("- ") and line[2:]]


def default_kind(objective: ObjectiveRecord) -> ObjectiveKind:
    return classify_objective(objective.title, objective.description)


def _bounded(value: int) -> int:
    return max(0, min(_MAX_PRIORITY, value))


async def _refine(
    env: Env, objective: ObjectiveRecord, kind: ObjectiveKind, base: list[TaskSpec]
) -> tuple[ObjectiveKind, PlanRefinement] | None:
    models = env.ctx.models
    if models is None or not env.limits.llm_refinement or env.limits.max_refinement_tasks <= 0:
        return None
    quoted = UntrustedContent(
        source=f"objective:{objective.id}", content=f"{objective.title}\n{objective.description}"
    ).quote(800)
    request = CompletionRequest(
        messages=(
            CompletionMessage(
                role="system",
                content=PLAN_REFINEMENT_SYSTEM.format(
                    max_extra=env.limits.max_refinement_tasks,
                    keys=", ".join(spec.key for spec in base),
                ),
            ),
            CompletionMessage(role="user", content=plan_refinement_user(kind.value, quoted)),
        ),
        max_tokens=1200,
        temperature=0,
        response_format="json_schema",
        json_schema=PlanRefinement.model_json_schema(),
        tier=ModelTier.STANDARD,
    )
    try:
        response = await models.complete(request, ModelCallContext(objective_id=objective.id))
        refinement = PlanRefinement.model_validate_json(response.text)
    except Exception as error:  # noqa: BLE001 - any provider or parse failure falls back
        await env.store.record_decision(
            "plan_refinement_skipped",
            f"deterministic plan used: {type(error).__name__}",
            key=f"orchestr:plan-refinement:{objective.id}",
            objective_id=objective.id,
        )
        return None
    return kind, refinement


async def build_objective_plan(env: Env, objective: ObjectiveRecord) -> PlanDocument:
    criteria = list(objective.completion_criteria)

    def template(kind: ObjectiveKind) -> list[TaskSpec]:
        return build_plan(kind, objective.title, objective.description, criteria)

    kind = default_kind(objective)
    base = validate_plan(template(kind), env.limits)
    refined = await _refine(env, objective, kind, base)
    if refined is None:
        return PlanDocument(kind=kind, tasks=tuple(base))
    _, refinement = refined
    # The plan kind (which routes the whole template) is decided by the deterministic classifier;
    # a model suggestion is only recorded, so the same objective always gets the same plan shape.
    suggested = parse_kind(refinement.kind)
    if suggested is not None and suggested is not kind:
        await env.store.record_decision(
            "plan_kind_suggestion_ignored",
            f"kept deterministic kind {kind.value}; model suggested {suggested.value}",
            key=f"orchestr:plan-kind:{objective.id}",
            objective_id=objective.id,
        )
    try:
        merged = apply_refinement(template(kind), refinement, env.limits)
    except PlanError as error:
        await env.store.record_decision(
            "plan_refinement_rejected",
            f"deterministic plan used: {error}",
            key=f"orchestr:plan-refinement:{objective.id}",
            objective_id=objective.id,
        )
        return PlanDocument(kind=kind, tasks=tuple(base))
    return PlanDocument(kind=kind, tasks=tuple(merged), source="template+llm")


async def load_plan(env: Env, objective: ObjectiveRecord) -> PlanDocument | None:
    stored = await env.store.get_decision(f"orchestr:plan:{objective.id}")
    if stored is None or not stored.evidence:
        return None
    try:
        plan = PlanDocument.model_validate_json(stored.evidence[0])
        validate_plan(plan.tasks, env.limits)
    except ValueError:
        return None
    return plan


async def plan_objective(env: Env, objective: ObjectiveRecord) -> dict[str, UUID]:
    """Create (or resume) the task DAG for ``objective``; safe to call any number of times."""
    plan = await load_plan(env, objective)
    if plan is None:
        plan = await build_objective_plan(env, objective)
        await env.store.record_decision(
            "plan_created",
            f"{plan.kind.value} plan with {len(plan.tasks)} tasks ({plan.source})",
            key=f"orchestr:plan:{objective.id}",
            evidence=[plan.model_dump_json()],
            objective_id=objective.id,
        )
    try:
        return await materialize(env, objective, plan.tasks)
    except PlanError as error:
        # Imported here: blockers -> assignment -> planning would otherwise be a cycle.
        from aether.agents.orchestr.blockers import PLAN, escalate_objective

        await escalate_objective(
            env,
            objective,
            key=f"plan-limit:{objective.id}",
            reason=f"plan not created: {error}",
            kind=PLAN,
        )
        return {}


async def materialize(
    env: Env, objective: ObjectiveRecord, specs: Sequence[TaskSpec]
) -> dict[str, UUID]:
    """Create missing tasks and dependency edges. Existing tasks are reused by idempotency key."""
    ordered = validate_plan(specs, env.limits)
    missing = [
        spec
        for spec in ordered
        if await env.store.task_by_key(plan_idempotency_key(objective.id, spec.key)) is None
    ]
    if missing:
        total = await env.store.count_tasks(objective.id) + len(missing)
        if total > env.limits.max_tasks_per_objective:
            raise PlanError(
                f"objective would have {total} tasks; limit is {env.limits.max_tasks_per_objective}"
            )
    ids: dict[str, UUID] = {}
    linked_requirement: UUID | None = None
    looked_up = False
    for spec in ordered:
        key = plan_idempotency_key(objective.id, spec.key)
        existing = await env.store.task_by_key(key)
        if existing is not None:
            ids[spec.key] = existing.id
            continue
        if not looked_up and spec.agent.value in LINKED_AGENTS:
            # Tasks planned after approval (rework, re-verification) inherit the objective's link.
            linked_requirement = await objective_requirement(env, objective.id)
            looked_up = True
        ids[spec.key] = await env.store.tasks.create(
            objective_id=objective.id,
            title=spec.title,
            description=render_description(spec),
            assigned_agent=spec.agent.value,
            priority=_bounded(objective.base_priority + spec.priority_offset),
            estimated_ram_mb=spec.ram_mb,
            estimated_disk_mb=spec.disk_mb,
            max_attempts=env.limits.max_task_attempts,
            requirement_id=linked_requirement if spec.agent.value in LINKED_AGENTS else None,
            deadline=objective.deadline,
            interruptible=spec.interruptible,
            estimated_cpu_percent=spec.cpu_percent,
            idempotency_key=key,
            actor=ACTOR,
        )
    for spec in ordered:
        for dependency in spec.depends_on:
            await env.store.dependencies.add(ids[spec.key], ids[dependency], actor=ACTOR)
    return ids


async def add_rework(env: Env, objective: ObjectiveRecord, rework_id: str) -> bool:
    """Append FORGE rework and LEDGER re-verification after a failed verification report.

    Returns False when the stored plan is unavailable or the extended plan breaks a limit.
    """
    plan = await load_plan(env, objective)
    if plan is None:
        return False
    by_key = {spec.key: spec for spec in plan.tasks}
    verification = by_key.get("verification")
    if verification is None:
        return False
    rework_key, reverify_key = f"rework.{rework_id}", f"reverify.{rework_id}"
    depends = tuple(key for key in ("security_review",) if key in by_key)
    rework = TaskSpec(
        key=rework_key,
        title=f"Rework after failed verification: {objective.title}"[:200],
        description="Fix the failures reported by independent verification.",
        agent=AgentId.FORGE,
        depends_on=depends,
        acceptance_criteria=("All failing checks reported by verification pass",),
        ram_mb=2048,
        disk_mb=2048,
        cpu_percent=60.0,
        interruptible=False,
    )
    reverify = verification.model_copy(
        update={
            "key": reverify_key,
            "title": f"Re-verify: {objective.title}"[:200],
            "depends_on": (rework_key,),
        }
    )
    specs = list(plan.tasks)
    try:
        await materialize(env, objective, [*specs, rework, reverify])
    except PlanError:
        return False
    return True
