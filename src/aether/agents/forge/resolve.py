"""Finding the plan for a task: a stored one, a deterministic template, or a validated LLM draft."""

from collections.abc import Sequence

from aether.agents.base import AgentContext
from aether.agents.forge.planning import build_template_plan, draft_with_llm, extract_spec
from aether.agents.forge.schemas import ForgeConfig, ForgePlan, PlanDefect, Refusal, RefusalCode
from aether.agents.forge.store import latest_plan, save_plan
from aether.storage.models import RequirementRecord, TaskRecord


async def load_requirement(ctx: AgentContext, task: TaskRecord) -> RequirementRecord | None:
    if task.requirement_id is None:
        return None
    async with ctx.sessions() as session:
        return await session.get(RequirementRecord, task.requirement_id)


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
    criteria = tuple(item.strip() for item in criteria_hint if item.strip()) or tuple(
        item.strip() for item in (requirement.acceptance_criteria if requirement else []) if item
    )
    try:
        spec = extract_spec(task.description, requirement.description if requirement else "")
        if spec is not None:
            plan = build_template_plan(
                spec, task, requirement_id=task.requirement_id, criteria=criteria, config=config
            )
        else:
            plan = await _draft(ctx, task, criteria, config)
    except PlanDefect as error:
        return None, Refusal(RefusalCode.PLAN_INVALID, str(error))
    if plan is None:
        return None, Refusal(
            RefusalCode.NO_PLAN,
            "no execution request or implementation plan exists and none could be derived",
        )
    await save_plan(ctx, task, plan)
    return plan, None


async def _draft(
    ctx: AgentContext, task: TaskRecord, criteria: tuple[str, ...], config: ForgeConfig
) -> ForgePlan | None:
    if ctx.tools is None:
        return None
    registered = {
        info.name: (info.description, info.input_schema) for info in ctx.tools.list_tools()
    }
    return await draft_with_llm(
        ctx,
        task,
        requirement_id=task.requirement_id,
        criteria=criteria,
        registered_tools=registered,
        config=config,
    )
