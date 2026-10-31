"""Finding the plan for a task: a stored one, a deterministic template, or a validated LLM draft."""

import re
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID

from sqlalchemy import select

from aether.agents.base import AgentContext
from aether.agents.forge.planning import build_template_plan, draft_with_llm, extract_spec
from aether.agents.forge.schemas import ForgeConfig, ForgePlan, PlanDefect, Refusal, RefusalCode
from aether.agents.forge.store import latest_plan, save_plan
from aether.agents.forge.templates import match_template
from aether.storage.models import RequirementRecord, RequirementStatus, TaskRecord

_ACTIONABLE = (RequirementStatus.APPROVED.value, RequirementStatus.IN_PROGRESS.value)
_CRITERIA_HEADER = re.compile(r"^\s*acceptance criteria:\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


async def load_requirement(ctx: AgentContext, task: TaskRecord) -> RequirementRecord | None:
    """The requirement ORCHESTR linked to the task.

    Only legacy tasks (created before ORCHESTR linked requirements) carry no ``requirement_id``;
    for those FORGE still falls back to the objective's latest approved capability requirement,
    and logs that it did.
    """
    async with ctx.sessions() as session:
        if task.requirement_id is not None:
            return await session.get(RequirementRecord, task.requirement_id)
        fallback = await session.scalar(
            select(RequirementRecord)
            .where(
                RequirementRecord.objective_id == task.objective_id,
                RequirementRecord.requirement_type == "capability",
                RequirementRecord.status.in_(_ACTIONABLE),
            )
            .order_by(RequirementRecord.created_at.desc(), RequirementRecord.id)
            .limit(1)
        )
    if fallback is not None:
        ctx.logger.warning(
            "forge.legacy_requirement_fallback: task %s has no linked requirement; using %s",
            task.id,
            fallback.id,
            extra={"task_id": str(task.id), "requirement_id": str(fallback.id)},
        )
    return fallback


def description_criteria(description: str) -> tuple[str, ...]:
    """Bullets under an ``Acceptance criteria:`` heading of a task description."""
    criteria: list[str] = []
    collecting = False
    for line in (description or "").splitlines():
        if _CRITERIA_HEADER.match(line):
            collecting = True
            continue
        if not collecting:
            continue
        bullet = _BULLET.match(line)
        if bullet is None:
            if line.strip():
                break
            continue
        criteria.append(bullet.group(1))
    return tuple(criteria)


def _clean(items: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (items or ()) if isinstance(item, str) and item.strip())


async def resolve_plan(
    ctx: AgentContext,
    task: TaskRecord,
    config: ForgeConfig,
    *,
    criteria_hint: Sequence[str] = (),
) -> tuple[ForgePlan | None, Refusal | None]:
    """Return a plan for ``task`` or a structured refusal explaining why none can be built."""
    stored = await latest_plan(ctx, task.id)
    if stored is not None:
        return stored, None
    requirement = await load_requirement(ctx, task)
    requirement_id = requirement.id if requirement is not None else task.requirement_id
    # The structured requirement is authoritative; the assignment hint and the task text are
    # fallbacks, so the plan does not depend on which of execution/assignment ran first.
    criteria = (
        _clean(requirement.acceptance_criteria if requirement else None)
        or _clean(criteria_hint)
        or description_criteria(task.description)
    )
    try:
        spec = extract_spec(task.description, requirement.description if requirement else "")
        if spec is not None:
            plan = build_template_plan(
                spec, task, requirement_id=requirement_id, criteria=criteria, config=config
            )
        else:
            plan = await _capability_or_draft(
                ctx, task, requirement, requirement_id, criteria, config
            )
    except PlanDefect as error:
        return None, Refusal(RefusalCode.PLAN_INVALID, str(error))
    if plan is None:
        return None, Refusal(
            RefusalCode.NO_PLAN,
            "no execution request or implementation plan exists and none could be derived",
        )
    await save_plan(ctx, task, plan)
    return plan, None


async def _capability_or_draft(
    ctx: AgentContext,
    task: TaskRecord,
    requirement: RequirementRecord | None,
    requirement_id: UUID | None,
    criteria: tuple[str, ...],
    config: ForgeConfig,
) -> ForgePlan | None:
    """A known capability uses its template; the LLM is only an optional refinement of it."""
    known = (
        match_template(
            task.title,
            task.description,
            requirement.title if requirement else None,
            requirement.description if requirement else None,
        )
        if config.capability_templates
        else None
    )
    if known is None:
        return await _draft(ctx, task, requirement_id, criteria, config)
    name, spec = known
    if config.llm_refine_templates:
        drafted = await _draft(ctx, task, requirement_id, criteria, config)
        if drafted is not None:
            return drafted
        ctx.logger.info("forge.template_fallback: using the %s template", name)
    plan = build_template_plan(
        spec, task, requirement_id=requirement_id, criteria=criteria, config=config
    )
    return replace(plan, source=f"template:{name}")


async def _draft(
    ctx: AgentContext,
    task: TaskRecord,
    requirement_id: UUID | None,
    criteria: tuple[str, ...],
    config: ForgeConfig,
) -> ForgePlan | None:
    if ctx.tools is None:
        return None
    registered = {
        info.name: (info.description, info.input_schema) for info in ctx.tools.list_tools()
    }
    return await draft_with_llm(
        ctx,
        task,
        requirement_id=requirement_id,
        criteria=criteria,
        registered_tools=registered,
        config=config,
    )
