"""Preflight: every prerequisite is checked before FORGE touches the workspace."""

from datetime import UTC, datetime
from uuid import UUID

from aether.agents.base import AgentContext
from aether.agents.forge.planning import scope_plan
from aether.agents.forge.schemas import (
    ForgeConfig,
    ForgePlan,
    PendingReview,
    PlanDefect,
    PreflightReport,
    Refusal,
    RefusalCode,
    RuntimeScopeDefect,
)
from aether.communication.contracts import BlockerKind
from aether.communication.messages import AgentId
from aether.security.operations import OperationKind
from aether.storage.models import RequirementRecord, RequirementStatus, TaskRecord, TaskStatus
from aether.storage.repositories import TaskDependencyRepository
from aether.storage.repositories.security import SecurityDecisionRepository
from aether.tools import ToolCallContext, ToolInputError, UnknownTool

# Mirrors the default-deny policy: these kinds always need a WARDEN decision for the exact input.
APPROVAL_KINDS = frozenset(
    {
        OperationKind.SUBPROCESS,
        OperationKind.HTTP_REQUEST,
        OperationKind.PACKAGE_INSTALL,
        OperationKind.REPO_CLONE,
        OperationKind.CONFIG_CHANGE,
        OperationKind.CREDENTIAL_ACCESS,
        OperationKind.NETWORK_SCAN,
    }
)
_INACTIVE = frozenset({RequirementStatus.REJECTED.value, RequirementStatus.SUPERSEDED.value})
_UNAPPROVED = frozenset(
    {
        RequirementStatus.PROPOSED.value,
        RequirementStatus.EVALUATING.value,
        RequirementStatus.NEEDS_EVIDENCE.value,
    }
)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def tool_call_context(task: TaskRecord) -> ToolCallContext:
    """Must match what `BaseAgent.invoke_tool` builds so decisions bind to the same hash."""
    return ToolCallContext(
        task_id=task.id, objective_id=task.objective_id, requirement_id=task.requirement_id
    )


class Preflight:
    def __init__(self, config: ForgeConfig) -> None:
        self._config = config

    async def check(
        self, task: TaskRecord, plan: ForgePlan | None, ctx: AgentContext
    ) -> PreflightReport:
        report = PreflightReport()
        async with ctx.sessions() as session:
            fresh = await session.get(TaskRecord, task.id, populate_existing=True)
        if fresh is None:
            report.refusals.append(Refusal(RefusalCode.TASK_MISSING, "the task does not exist"))
            return report
        report.task = fresh
        self._scheduler(fresh, ctx, report)
        if plan is None:
            report.refusals.append(
                Refusal(RefusalCode.NO_PLAN, "no implementation plan is available for this task")
            )
            return report
        requirement = await self._links(fresh, plan, report, ctx)
        report.requirement_id = fresh.requirement_id or plan.requirement_id
        report.criteria = self._criteria(plan, requirement, report)
        scoped = self._scope(plan, report)
        if scoped is not None:
            report.plan = scoped
            self._shape(scoped, ctx, report)
            await self._dependencies(fresh, plan, ctx, report)
            self._resources(fresh, scoped, report)
        if not report.refusals and report.plan is not None:
            await self._security(fresh, report.plan, ctx, report)
        return report

    async def check_plan_shape(
        self, task: TaskRecord, plan: ForgePlan, ctx: AgentContext
    ) -> PreflightReport:
        """Static checks only (no scheduler or security state), for plans that merely arrive."""
        report = PreflightReport(task=task)
        requirement = await self._links(task, plan, report, ctx)
        report.criteria = self._criteria(plan, requirement, report)
        scoped = self._scope(plan, report)
        if scoped is not None:
            self._shape(scoped, ctx, report)
            self._resources(task, scoped, report)
        return report

    def _scheduler(self, task: TaskRecord, ctx: AgentContext, report: PreflightReport) -> None:
        if task.assigned_agent not in (None, AgentId.FORGE.value):
            report.refusals.append(
                Refusal(RefusalCode.NOT_ASSIGNED, f"task is assigned to {task.assigned_agent}")
            )
        if task.state != TaskStatus.RUNNING.value:
            report.refusals.append(
                Refusal(RefusalCode.NOT_RUNNING, f"scheduler state is {task.state!r}, not running")
            )
            return
        expires = _aware(task.lease_expires_at)
        if not task.lease_owner or expires is None or expires <= ctx.clock():
            report.refusals.append(
                Refusal(RefusalCode.NO_LEASE, "the task has no live scheduler lease")
            )

    async def _links(
        self, task: TaskRecord, plan: ForgePlan, report: PreflightReport, ctx: AgentContext
    ) -> RequirementRecord | None:
        if plan.task_id != task.id:
            report.refusals.append(
                Refusal(RefusalCode.PLAN_INVALID, "the plan belongs to a different task")
            )
        if plan.objective_id is not None and plan.objective_id != task.objective_id:
            report.refusals.append(
                Refusal(RefusalCode.OBJECTIVE_MISMATCH, "the plan objective differs from the task")
            )
        if (
            task.requirement_id
            and plan.requirement_id
            and task.requirement_id != plan.requirement_id
        ):
            report.refusals.append(
                Refusal(
                    RefusalCode.OBJECTIVE_MISMATCH, "the plan requirement differs from the task"
                )
            )
        requirement_id = task.requirement_id or plan.requirement_id
        if requirement_id is None:
            return None
        async with ctx.sessions() as session:
            requirement = await session.get(RequirementRecord, requirement_id)
        if requirement is None:
            report.refusals.append(
                Refusal(RefusalCode.NO_REQUIREMENT, "the linked requirement does not exist")
            )
        elif requirement.status in _INACTIVE:
            report.refusals.append(
                Refusal(
                    RefusalCode.REQUIREMENT_INACTIVE,
                    f"the linked requirement is {requirement.status}",
                )
            )
        elif requirement.status in _UNAPPROVED:
            report.refusals.append(
                Refusal(
                    RefusalCode.REQUIREMENT_NOT_APPROVED,
                    f"the linked requirement is {requirement.status}, not approved",
                )
            )
        elif requirement.objective_id != task.objective_id:
            report.refusals.append(
                Refusal(
                    RefusalCode.OBJECTIVE_MISMATCH, "the requirement belongs to another objective"
                )
            )
        return requirement

    def _criteria(
        self, plan: ForgePlan, requirement: RequirementRecord | None, report: PreflightReport
    ) -> tuple[str, ...]:
        own = tuple(item.strip() for item in plan.acceptance_criteria if item.strip())
        inherited = tuple(
            item.strip()
            for item in (requirement.acceptance_criteria if requirement else [])
            if item
        )
        criteria = own or inherited
        if not criteria:
            report.refusals.append(
                Refusal(RefusalCode.NO_ACCEPTANCE_CRITERIA, "no acceptance criteria are defined")
            )
        return criteria

    def _scope(self, plan: ForgePlan, report: PreflightReport) -> ForgePlan | None:
        try:
            scoped = scope_plan(plan, self._config)
        except RuntimeScopeDefect as error:
            report.refusals.append(Refusal(RefusalCode.RUNTIME_SCOPE, str(error)))
        except PlanDefect as error:
            report.refusals.append(Refusal(RefusalCode.PLAN_INVALID, str(error)))
        else:
            if scoped.runtime_change and (plan.requirement_id or report.requirement_id) is None:
                report.refusals.append(
                    Refusal(
                        RefusalCode.RUNTIME_NEEDS_REQUIREMENT,
                        "runtime self-changes must be tied to an approved requirement",
                    )
                )
            return scoped
        return None

    def _shape(self, plan: ForgePlan, ctx: AgentContext, report: PreflightReport) -> None:
        limit = self._config.max_steps
        if not plan.steps or len(plan.steps) > limit or len(plan.rollback_steps) > limit:
            report.refusals.append(
                Refusal(RefusalCode.TOO_MANY_STEPS, f"plans need 1 to {limit} steps")
            )
        registered = {info.name for info in ctx.tools.list_tools()} if ctx.tools else set()
        unknown = sorted(
            {
                step.tool
                for step in (*plan.steps, *plan.rollback_steps)
                if step.tool not in registered
            }
        )
        if unknown:
            report.refusals.append(
                Refusal(RefusalCode.UNKNOWN_TOOL, f"unregistered tools: {', '.join(unknown)}")
            )

    async def _dependencies(
        self, task: TaskRecord, plan: ForgePlan, ctx: AgentContext, report: PreflightReport
    ) -> None:
        edges = await TaskDependencyRepository(ctx.sessions).list_dependencies(task.id)
        wanted: list[UUID] = [*plan.depends_on, *edges]
        incomplete = []
        async with ctx.sessions() as session:
            for dependency_id in dict.fromkeys(wanted):
                dependency = await session.get(TaskRecord, dependency_id, populate_existing=True)
                if dependency is None or dependency.state != TaskStatus.COMPLETED.value:
                    incomplete.append(str(dependency_id))
        if incomplete:
            report.refusals.append(
                Refusal(
                    RefusalCode.DEPENDENCY_INCOMPLETE,
                    f"dependencies not completed: {', '.join(incomplete)}",
                )
            )

    def _resources(self, task: TaskRecord, plan: ForgePlan, report: PreflightReport) -> None:
        estimate, config = plan.resource_estimate, self._config
        ram = max(estimate.ram_mb, task.estimated_ram_mb)
        disk = max(estimate.disk_mb, task.estimated_disk_mb)
        cpu = max(estimate.cpu_percent, task.estimated_cpu_percent or 0.0)
        duration = estimate.duration_s or task.estimated_duration_seconds or 0
        if not (ram or disk or cpu or duration):
            report.refusals.append(
                Refusal(RefusalCode.NO_RESOURCE_ESTIMATE, "the plan has no resource estimate")
            )
            return
        over = [
            name
            for name, value, limit in (
                ("ram_mb", ram, config.max_ram_mb),
                ("disk_mb", disk, config.max_disk_mb),
                ("cpu_percent", cpu, config.max_cpu_percent),
                ("duration_s", duration, config.max_duration_s),
            )
            if value > limit
        ]
        if over:
            report.refusals.append(
                Refusal(
                    RefusalCode.RESOURCE_EXCEEDED,
                    f"estimate exceeds FORGE limits: {', '.join(over)}",
                    kind=BlockerKind.RESOURCE,
                )
            )

    async def _security(
        self, task: TaskRecord, plan: ForgePlan, ctx: AgentContext, report: PreflightReport
    ) -> None:
        if ctx.tools is None:
            return
        kinds = {info.name: info.kind for info in ctx.tools.list_tools()}
        repository = SecurityDecisionRepository(ctx.sessions)
        call = tool_call_context(task)
        now = ctx.clock()
        seen: set[str] = set()
        for step in (*plan.steps, *plan.rollback_steps):
            if kinds.get(step.tool) not in APPROVAL_KINDS:
                continue
            try:
                operation = ctx.tools.build_operation(step.tool, step.arguments, call)
            except (ToolInputError, UnknownTool) as error:
                report.refusals.append(Refusal(RefusalCode.PLAN_INVALID, str(error)))
                continue
            if operation.input_hash in seen:
                continue
            seen.add(operation.input_hash)
            decisions = [
                d
                for d in await repository.list_for_input_hash(operation.input_hash)
                if d.scope.get("decided_by") == AgentId.WARDEN.value
            ]
            latest = decisions[-1] if decisions else None
            if latest is not None and latest.decision == "rejected":
                report.refusals.append(
                    Refusal(
                        RefusalCode.SECURITY_REJECTED,
                        f"WARDEN rejected {operation.describe()}",
                        kind=BlockerKind.SECURITY,
                        route_to=AgentId.WARDEN,
                    )
                )
            elif latest is None:
                report.pending.append(PendingReview(operation, "no WARDEN decision"))
            elif latest.decision != "approved":
                report.pending.append(PendingReview(operation, f"WARDEN: {latest.decision}"))
            else:
                expires = _aware(latest.expires_at)
                if expires is None or expires <= now:
                    report.pending.append(PendingReview(operation, "the approval has expired"))
