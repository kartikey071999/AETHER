"""FORGE workflow: resolve a plan, pass preflight, execute with a journal, roll back, report."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.forge.failure import classify_failure, is_retryable, route_for
from aether.agents.forge.outbound import (
    blocker_message,
    evaluate_message,
    promotion_message,
    result_message,
    verification_message,
)
from aether.agents.forge.planning import manifest_step
from aether.agents.forge.preflight import Preflight
from aether.agents.forge.resolve import resolve_plan
from aether.agents.forge.schemas import (
    PROMOTION_PATH,
    FailureClass,
    ForgeConfig,
    ForgePlan,
    PreflightReport,
    RefusalCode,
)
from aether.agents.forge.store import Journal
from aether.communication.contracts import PlanStep
from aether.storage.models import RunStatus, TaskRecord

logger = logging.getLogger("aether.agent.forge")
Sleep = Callable[[float], Awaitable[None]]
_TAIL_CHARS = 4000


@dataclass(slots=True)
class StepOutcome:
    ok: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    failure_class: FailureClass | None = None
    error: str = ""
    waiting: TaskResult | None = None


def _clip(text: object, limit: int) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: max(limit - 3, 0)] + "..."


def _command(step: PlanStep, limit: int) -> str:
    argv = step.arguments.get("argv")
    if isinstance(argv, list):
        return " ".join(_clip(item, 80) for item in argv)[:limit]
    return ""


def _evidence(step: PlanStep, value: Any, limit: int) -> dict[str, Any]:
    """Facts about one finished tool call; external output stays quoted data, never instructions."""
    base: dict[str, Any] = {"type": "step", "tool": step.tool, "purpose": _clip(step.purpose, 200)}
    if hasattr(value, "exit_code"):
        base.update(
            type="process",
            command=_command(step, limit),
            cwd=step.arguments.get("cwd"),
            run_id=str(value.run_id),
            exit_code=value.exit_code,
            timed_out=bool(value.timed_out),
            duration_seconds=round(float(value.duration_seconds), 3),
            stdout=value.stdout.excerpt(limit),
            stderr=value.stderr.excerpt(limit),
            stdout_sha256=value.stdout.content_hash,
            stdout_path=str(value.stdout_path),
            stderr_path=str(value.stderr_path),
            output_flags=sorted(
                {*value.stdout.suspicious_patterns(), *value.stderr.suspicious_patterns()}
            ),
        )
    elif hasattr(value, "bytes_written"):
        base.update(
            type="write",
            target=step.arguments.get("path"),
            path=str(value.path),
            bytes_written=value.bytes_written,
            sha256=value.sha256,
        )
    elif hasattr(value, "size"):
        base.update(
            type="read",
            target=step.arguments.get("path"),
            path=str(value.path),
            size=value.size,
            content_sha256=value.content.content_hash,
        )
    return base


class ForgeWorkflow:
    def __init__(self, agent: BaseAgent, config: ForgeConfig, sleep: Sleep = asyncio.sleep):
        self._agent = agent
        self._config = config
        self._sleep = sleep

    async def run(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        plan, refusal = await resolve_plan(ctx, task, self._config)
        if plan is not None:
            journal = await Journal(ctx, task, plan.plan_id).load()
            final = journal.final()
            if final is not None:
                return TaskResult.model_validate(final["result"])
        report = await Preflight(self._config).check(task, plan, ctx)
        if refusal is not None and plan is None:
            report.refusals = [
                refusal if item.code is RefusalCode.NO_PLAN else item for item in report.refusals
            ]
        if report.refusals:
            return await self._refuse(task, report, ctx)
        if report.pending:
            return await self._await_review(task, report, ctx)
        assert report.task is not None and report.plan is not None and plan is not None
        journal = await Journal(ctx, report.task, plan.plan_id).load()
        return await self._execute(report.task, report.plan, report, journal, ctx)

    async def _refuse(
        self, task: TaskRecord, report: PreflightReport, ctx: AgentContext
    ) -> TaskResult:
        first = report.refusals[0]
        codes = [item.code.value for item in report.refusals]
        description = "; ".join(f"{item.code.value}: {item.detail}" for item in report.refusals)
        if report.task is not None:
            await ctx.bus.publish(
                blocker_message(
                    report.task,
                    kind=first.kind,
                    route_to=first.route_to,
                    description=f"FORGE preflight refused the task: {description}",
                    key=f"preflight:{report.task.attempt}:{'+'.join(codes)}",
                    attempts=report.task.attempt,
                    evidence=[
                        {"type": "preflight", "refusals": [i.as_dict() for i in report.refusals]}
                    ],
                )
            )
        return TaskResult(
            status=TaskResultStatus.BLOCKED,
            summary="preflight refused the task; nothing was executed",
            outputs={
                "preflight": "refused",
                "refusal_codes": codes,
                "refusals": [item.as_dict() for item in report.refusals],
            },
            error=description,
            retryable=False,
        )

    async def _await_review(
        self, task: TaskRecord, report: PreflightReport, ctx: AgentContext
    ) -> TaskResult:
        current = report.task or task
        message_ids = []
        for review in report.pending:
            message = evaluate_message(current, review.operation, review.reason)
            await ctx.bus.publish(message)
            message_ids.append(str(message.message_id))
        return TaskResult(
            status=TaskResultStatus.WAITING_FOR_APPROVAL,
            summary="waiting for WARDEN decisions on the plan's operations; nothing was executed",
            outputs={
                "preflight": "awaiting_security_clearance",
                "pending_operations": [
                    {
                        "operation": item.operation.describe(),
                        "kind": item.operation.kind.value,
                        "input_hash": item.operation.input_hash,
                        "reason": item.reason,
                    }
                    for item in report.pending
                ],
                "review_message_ids": message_ids,
            },
        )

    async def _execute(
        self,
        task: TaskRecord,
        plan: ForgePlan,
        report: PreflightReport,
        journal: Journal,
        ctx: AgentContext,
    ) -> TaskResult:
        failure = journal.abandoned()
        if failure is None:
            ordered: list[tuple[str, int, PlanStep]] = [("init", 0, manifest_step(plan))]
            ordered += [("steps", index, step) for index, step in enumerate(plan.steps)]
            for phase, index, step in ordered:
                if journal.succeeded(phase, index):
                    continue
                if self._agent.is_cancelled(task.id):
                    return TaskResult(
                        status=TaskResultStatus.FAILED,
                        summary="FORGE stopped: the task was cancelled",
                        error="cancelled",
                        retryable=False,
                    )
                outcome = await self._run_step(task, ctx, journal, phase, index, step, retry=True)
                if outcome.waiting is not None:
                    return self._waiting(outcome.waiting, plan)
                if not outcome.ok:
                    failure = await self._abandon(journal, phase, index, outcome)
                    break
        if failure is None and plan.runtime_change and not journal.succeeded("promotion", 0):
            step = self._promotion_step(task, plan, report, journal)
            outcome = await self._run_step(task, ctx, journal, "promotion", 0, step, retry=True)
            if outcome.waiting is not None:
                return self._waiting(outcome.waiting, plan)
            if not outcome.ok:
                failure = await self._abandon(journal, "promotion", 0, outcome)
        if failure is not None:
            return await self._fail(task, plan, journal, failure, ctx)
        return await self._succeed(task, plan, report, journal, ctx)

    @staticmethod
    def _waiting(result: TaskResult, plan: ForgePlan) -> TaskResult:
        outputs = {**result.outputs, "plan_id": str(plan.plan_id), "preflight": "cleared"}
        return result.model_copy(update={"outputs": outputs})

    async def _run_step(
        self,
        task: TaskRecord,
        ctx: AgentContext,
        journal: Journal,
        phase: str,
        index: int,
        step: PlanStep,
        *,
        retry: bool,
    ) -> StepOutcome:
        while True:
            prior_failures = journal.failures(phase, index)
            await journal.append(phase, index, "started", tool=step.tool)
            outcome = await self._attempt(task, ctx, step)
            if outcome.ok:
                await journal.append(
                    phase,
                    index,
                    "succeeded",
                    evidence=outcome.evidence,
                    run_id=outcome.evidence.get("run_id"),
                )
                return outcome
            if outcome.waiting is not None:
                await journal.append(phase, index, "waiting", evidence=outcome.evidence)
                return outcome
            assert outcome.failure_class is not None
            await journal.append(
                phase,
                index,
                "failed",
                failure_class=outcome.failure_class.value,
                error=_clip(outcome.error, 1000),
                evidence=outcome.evidence,
                run_id=outcome.evidence.get("run_id"),
            )
            retries_left = prior_failures + 1 <= self._config.max_step_retries
            if retry and is_retryable(outcome.failure_class) and retries_left:
                await self._sleep(self._config.backoff(prior_failures))
                continue
            return outcome

    async def _attempt(self, task: TaskRecord, ctx: AgentContext, step: PlanStep) -> StepOutcome:
        limit = self._config.evidence_chars
        base = {"type": "step", "tool": step.tool, "purpose": _clip(step.purpose, 200)}
        try:
            invocation = await self._agent.invoke_tool(step.tool, step.arguments, ctx, task=task)
        except Exception as error:  # noqa: BLE001 - adapters may raise OS errors directly
            text = f"{type(error).__name__}: {error}"
            return StepOutcome(False, base, classify_failure(text), _clip(text, 1000))
        if not invocation.ok:
            result = invocation.result
            assert result is not None
            evidence = {
                **base,
                "outcome": result.status.value,
                "detail": _clip(result.summary, 300),
            }
            if result.status is TaskResultStatus.WAITING_FOR_APPROVAL:
                return StepOutcome(False, evidence, waiting=result)
            if result.status is TaskResultStatus.BLOCKED:
                return StepOutcome(False, evidence, FailureClass.SECURITY, result.error or "")
            if not result.retryable:
                return StepOutcome(False, evidence, FailureClass.PLAN_DEFECT, result.error or "")
            text = result.error or result.summary
            kind = classify_failure(text)
            kind = FailureClass.ENVIRONMENT if kind is FailureClass.UNKNOWN else kind
            return StepOutcome(False, evidence, kind, text)
        value = invocation.value
        evidence = _evidence(step, value, limit)
        if hasattr(value, "exit_code") and not getattr(value, "succeeded", False):
            text = f"{value.stderr.content}\n{value.stdout.content}"[-_TAIL_CHARS:]
            kind = classify_failure(text, timed_out=bool(value.timed_out))
            reason = "timed out" if value.timed_out else f"exit code {value.exit_code}"
            return StepOutcome(False, evidence, kind, f"{reason}: {_clip(text.strip(), 600)}")
        return StepOutcome(True, evidence)

    @staticmethod
    async def _abandon(
        journal: Journal, phase: str, index: int, outcome: StepOutcome
    ) -> dict[str, Any]:
        assert outcome.failure_class is not None
        return await journal.append(
            phase,
            index,
            "abandoned",
            failure_class=outcome.failure_class.value,
            error=_clip(outcome.error, 1000),
            evidence=outcome.evidence,
        )

    def _promotion_step(
        self, task: TaskRecord, plan: ForgePlan, report: PreflightReport, journal: Journal
    ) -> PlanStep:
        files = [
            {
                "path": item["target"],
                "sha256": item.get("sha256"),
                "bytes": item.get("bytes_written"),
            }
            for item in journal.evidence()
            if item.get("type") == "write"
            and str(item.get("target", "")).startswith(f"{task.id}/runtime-copy/")
        ]
        content = json.dumps(
            {
                "task_id": str(task.id),
                "plan_id": str(plan.plan_id),
                "requirement_id": str(report.requirement_id) if report.requirement_id else None,
                "workspace_copy": f"{task.id}/runtime-copy",
                "files": files,
                "acceptance_criteria": list(report.criteria),
                "status": "awaiting_verification_and_review",
                "verified": False,
                "deploy": False,
            },
            indent=2,
            sort_keys=True,
        )
        return PlanStep(
            tool="write_file",
            arguments={"path": f"{task.id}/{PROMOTION_PATH}", "content": content, "append": False},
            purpose="Record the promotion request for the runtime self-change",
        )

    async def _fail(
        self,
        task: TaskRecord,
        plan: ForgePlan,
        journal: Journal,
        failure: dict[str, Any],
        ctx: AgentContext,
    ) -> TaskResult:
        rolled_back = rollback_failed = 0
        for index, step in enumerate(plan.rollback_steps):
            if journal.succeeded("rollback", index):
                rolled_back += 1
                continue
            outcome = await self._run_step(task, ctx, journal, "rollback", index, step, retry=False)
            if outcome.ok:
                rolled_back += 1
            else:
                rollback_failed += 1
        failure_class = FailureClass(failure["failure_class"])
        route = route_for(failure_class)
        where = f"{failure['phase']}[{failure['index']}]"
        attempts = journal.failures(failure["phase"], failure["index"])
        error = f"step {where} failed ({failure_class.value}): {failure.get('error', '')}"
        evidence = journal.evidence()
        description = (
            f"{error}. Rolled back {rolled_back} of {len(plan.rollback_steps)} rollback step(s)"
            + (f"; {rollback_failed} rollback step(s) failed" if rollback_failed else "")
        )
        await ctx.bus.publish(
            blocker_message(
                task,
                kind=route.kind,
                route_to=route.route_to,
                description=description,
                key=f"failure:{plan.plan_id}",
                attempts=attempts,
                evidence=evidence[-5:],
            )
        )
        outputs = {
            "plan_id": str(plan.plan_id),
            "failure_class": failure_class.value,
            "failed_step": where,
            "blocker_kind": route.kind.value,
            "routed_to": route.route_to.value,
            "rollback": {"executed": rolled_back, "failed": rollback_failed},
            "execution_run_ids": journal.run_ids(),
        }
        await ctx.bus.publish(
            result_message(
                task,
                plan,
                status=RunStatus.FAILED,
                run_ids=journal.run_ids(),
                artifacts=self._artifacts(journal),
                evidence=evidence,
                outputs=outputs,
                error=error,
            )
        )
        result = TaskResult(
            status=TaskResultStatus.BLOCKED,
            summary=f"{error}; blocker routed to {route.route_to.value}",
            outputs=outputs,
            evidence=evidence,
            error=error,
            retryable=False,
        )
        await journal.append("final", 0, "failed", result=result.model_dump(mode="json"))
        return result

    @staticmethod
    def _artifacts(journal: Journal) -> list[str]:
        paths = [
            item["path"]
            for item in journal.evidence()
            if item.get("type") == "write" and isinstance(item.get("path"), str)
        ]
        return list(dict.fromkeys(paths))

    async def _succeed(
        self,
        task: TaskRecord,
        plan: ForgePlan,
        report: PreflightReport,
        journal: Journal,
        ctx: AgentContext,
    ) -> TaskResult:
        evidence = journal.evidence()
        run_ids = journal.run_ids()
        artifacts = self._artifacts(journal)
        requirement_id: UUID | None = report.requirement_id
        outputs: dict[str, Any] = {
            "plan_id": str(plan.plan_id),
            "plan_source": plan.source,
            "workspace": str(task.id),
            "steps_completed": len(plan.steps),
            "artifacts": artifacts,
            "execution_run_ids": run_ids,
            "runtime_change": plan.runtime_change,
            "deployed": False,
            "verified": False,
        }
        await ctx.bus.publish(
            result_message(
                task,
                plan,
                status=RunStatus.SUCCEEDED,
                run_ids=run_ids,
                artifacts=artifacts,
                evidence=evidence,
                outputs=outputs,
            )
        )
        outputs["verification_requested"] = requirement_id is not None
        if requirement_id is not None:
            await ctx.bus.publish(
                verification_message(
                    task,
                    plan,
                    requirement_id=requirement_id,
                    criteria=report.criteria,
                    run_ids=run_ids,
                    artifacts=artifacts,
                    evidence=evidence,
                )
            )
        outputs["promotion_requested"] = plan.runtime_change
        if plan.runtime_change:
            await ctx.bus.publish(
                promotion_message(
                    task,
                    plan,
                    details={
                        "workspace_copy": f"{task.id}/runtime-copy",
                        "promotion_artifact": f"{task.id}/{PROMOTION_PATH}",
                        "artifacts": artifacts,
                        "verification_required": True,
                        "deploys": False,
                    },
                )
            )
        result = TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary=(
                f"executed {len(plan.steps)} step(s) in workspace {task.id}; "
                "submitted for independent verification (not self-verified)"
            ),
            outputs=outputs,
            evidence=evidence,
        )
        await journal.append("final", 0, "succeeded", result=result.model_dump(mode="json"))
        return result
