"""LEDGER: operational records, the requirements ledger, and independent verification.

LEDGER never trusts a success claim. A capability moves ``installed -> tested -> verified`` only
after LEDGER itself ran the acceptance suite (``python -m pytest`` through the allowlisted
``run_process`` tool) and every acceptance criterion passed with test evidence. Everything here
is deterministic; LEDGER makes no LLM calls.
"""

import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.ledger.gating import verification_progress, waiting_verification_tasks
from aether.agents.ledger.queries import LedgerQueries
from aether.agents.ledger.records import TestRunStore, existing_id
from aether.agents.ledger.workflow import (
    CriterionResult,
    ParsedRun,
    UnsafeSuite,
    Verdict,
    VerificationSpec,
    decide_verdict,
    evaluate_criteria,
    explicit_criterion_tests,
    failing_tests,
    merged_counts,
    parse_pytest_output,
    parse_suite,
    pytest_argv,
    safe_workspace,
    verdict_for_status,
)
from aether.agents.work_queue import task_ref_in
from aether.communication.contracts import (
    AcceptanceResult,
    BlockerKind,
    BlockerPayload,
    CapabilityRegisterPayload,
    CapabilityVerification,
    EvaluationOutcome,
    ExecutionResultPayload,
    Payload,
    RequirementEvaluatedPayload,
    RequirementSubmitPayload,
    SecurityDecisionPayload,
    TestCaseResult,
    TestCounts,
    TestReportPayload,
    VerificationRequestPayload,
    VerificationVerdict,
    build_message,
    receivable_types,
    validate_message,
    validate_payload,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security import DecisionOutcome
from aether.storage.models import (
    CapabilityRecord,
    CapabilityStatus,
    ErrorRecord,
    EventCategory,
    MessageRecord,
    ObjectiveRecord,
    RequirementRecord,
    RequirementStatus,
    ResearchFindingRecord,
    ResultOutcome,
    RunStatus,
    TaskRecord,
    TaskStatus,
    TestRunRecord,
)
from aether.storage.repositories import (
    CapabilityRepository,
    ErrorRepository,
    InvalidTaskTransition,
    RequirementRepository,
    TaskRepository,
    add_event,
)

# Deterministic ids for everything LEDGER publishes or creates, so redelivery is idempotent.
LEDGER_NAMESPACE = UUID("2a8f6c1e-5b7d-4e0a-9c3f-71d2e8b4a610")
RUN_PROCESS_TOOL = "run_process"
VERIFICATION_ERROR_TYPES = ("verification_failed", "verification_regression")
OPEN_REQUIREMENT_STATES = (
    RequirementStatus.PROPOSED.value,
    RequirementStatus.APPROVED.value,
    RequirementStatus.IN_PROGRESS.value,
)
_ACTOR = AgentId.LEDGER.value


def _derived_id(*parts: object) -> UUID:
    return uuid5(LEDGER_NAMESPACE, ":".join(str(part) for part in parts))


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _suite_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        joined = " ".join(item.strip() for item in value if item.strip())
        return joined or None
    return None


def _criteria(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _workspace_from(items: Sequence[Mapping[str, Any]], *extra: Mapping[str, Any]) -> object:
    for item in (*extra, *items):
        if isinstance(item, Mapping) and item.get("workspace") is not None:
            return item["workspace"]
    return None


def _message_from_row(row: MessageRecord) -> AgentMessage:
    return AgentMessage(
        message_id=row.id,
        source_agent=AgentId(row.source_agent),
        target_agent=AgentId(row.target_agent),
        message_type=MessageType(row.message_type),
        objective_id=row.objective_id,
        task_id=row.task_id,
        requirement_id=row.requirement_id,
        correlation_id=row.correlation_id,
        timestamp=_utc(row.timestamp),
        payload=row.payload,
        priority=row.priority,
        deadline=_utc(row.deadline) if row.deadline else None,
        requires_response=row.requires_response,
    )


def _utc(value: datetime) -> datetime:
    """SQLite returns naive timestamps; they are stored as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class LedgerAgent(BaseAgent):
    """Records keeper and independent verifier; registers capabilities only on evidence."""

    agent_id = AgentId.LEDGER
    guard_terminal_objectives = True
    accepted_message_types = receivable_types(AgentId.LEDGER)

    def __init__(
        self, *, python_executable: str | None = None, research_escalation_after: int = 3
    ) -> None:
        super().__init__()
        self._python = python_executable or sys.executable
        self._research_escalation_after = max(research_escalation_after, 1)
        # Specs of runs waiting for WARDEN, for messages that never reached the bus table.
        self._specs: dict[UUID, VerificationSpec] = {}

    def queries(self, ctx: AgentContext) -> LedgerQueries:
        """The read-only operational-visibility service over ``ctx.sessions``."""
        return LedgerQueries(ctx.sessions, clock=ctx.clock)

    # -- message handling --------------------------------------------------------------------

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        if message.target_agent is not self.agent_id:
            raise ValueError(f"message addressed to {message.target_agent.value}, not LEDGER")
        payload = validate_message(message)
        if isinstance(payload, VerificationRequestPayload):
            spec = await self._spec_from_request(message, payload, ctx)
            await self._mark_installed(ctx, spec.capability_id, payload.execution_run_ids)
            await self.verify(spec, ctx)
        elif isinstance(payload, ExecutionResultPayload):
            await self._on_execution_result(message, payload, ctx)
        elif isinstance(payload, SecurityDecisionPayload):
            await self._on_security_decision(payload, ctx)
        elif isinstance(payload, RequirementSubmitPayload):
            await self._on_requirement_submit(message, payload, ctx)
        elif isinstance(payload, RequirementEvaluatedPayload):
            await self._on_requirement_evaluated(payload, ctx)
        else:
            ctx.logger.info(
                "LEDGER recorded %s from %s",
                message.message_type.value,
                message.source_agent.value,
                extra={"message_id": str(message.message_id)},
            )

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        """Answer the operational snapshot; a verification task first needs a real test run."""
        progress = await verification_progress(ctx.sessions, self.work_queue(ctx), task.id)
        if progress is not None and not progress.finished and task.requirement_id is not None:
            return TaskResult(
                status=TaskResultStatus.WAITING_FOR_VERIFICATION,
                summary="waiting for the independent test run of the implementation",
                outputs={"awaiting_test_run_for": [str(item) for item in progress.forge_task_ids]},
            )
        snapshot = await self.queries(ctx).snapshot()
        outputs: dict[str, Any] = {"snapshot": snapshot.model_dump(mode="json")}
        summary = "operational records snapshot"
        if progress is not None and progress.run is not None:
            outputs["test_run_id"] = str(progress.run.id)
            outputs["verdict"] = progress.run.verdict
            summary = f"independent verification finished: {progress.run.verdict}"
        return TaskResult(status=TaskResultStatus.COMPLETED, summary=summary, outputs=outputs)

    async def tick(self, ctx: AgentContext) -> list[UUID]:
        """Release verification tasks whose test run has a verdict; returns the resumed ids."""
        return await self._resume_verification_tasks(ctx)

    async def _resume_verification_tasks(self, ctx: AgentContext) -> list[UUID]:
        resumed: list[UUID] = []
        work = self.work_queue(ctx)
        for task in await waiting_verification_tasks(work):
            progress = await verification_progress(ctx.sessions, work, task.id)
            if progress is None or not progress.finished:
                continue
            try:
                await TaskRepository(ctx.sessions).transition(
                    task.id,
                    TaskStatus.READY,
                    actor=_ACTOR,
                    details={"test_run_id": str(progress.run.id) if progress.run else None},
                )
            except (InvalidTaskTransition, LookupError):
                continue
            resumed.append(task.id)
        return resumed

    # -- verification specs ------------------------------------------------------------------

    async def _spec_from_request(
        self, message: AgentMessage, payload: VerificationRequestPayload, ctx: AgentContext
    ) -> VerificationSpec:
        problem: str | None = None
        try:
            workspace = safe_workspace(_workspace_from(payload.evidence))
        except UnsafeSuite as error:
            workspace, problem = None, str(error)
        requirement_id = payload.requirement_id or message.requirement_id
        capability_id = payload.capability_id or await self._capability_for(ctx, requirement_id)
        return VerificationSpec(
            source_message_id=message.message_id,
            suite=payload.suite,
            acceptance_criteria=tuple(payload.acceptance_criteria),
            task_id=payload.task_id or message.task_id,
            capability_id=capability_id,
            requirement_id=requirement_id,
            objective_id=message.objective_id,
            execution_run_ids=tuple(payload.execution_run_ids),
            criterion_tests=explicit_criterion_tests(payload.evidence),
            workspace=workspace,
            correlation_id=message.correlation_id,
            problem=problem,
        )

    async def _spec_from_execution(
        self, message: AgentMessage, payload: ExecutionResultPayload, ctx: AgentContext
    ) -> VerificationSpec | None:
        outputs = payload.outputs
        task = await self.work_queue(ctx).task_detail(payload.task_id)
        async with ctx.sessions() as session:
            requirement_id = (
                payload.requirement_id
                or message.requirement_id
                or (task.requirement_id if task else None)
            )
            requirement = (
                await session.get(RequirementRecord, requirement_id) if requirement_id else None
            )
        capability_id = _as_uuid(outputs.get("capability_id")) or await self._capability_for(
            ctx, requirement_id
        )
        suite = _suite_text(outputs.get("test_suite")) or _suite_text(outputs.get("suite"))
        if suite is None and capability_id is None and requirement_id is None:
            return None  # Not capability work: nothing LEDGER can verify.
        criteria = _criteria(outputs.get("acceptance_criteria")) or _criteria(
            requirement.acceptance_criteria if requirement else None
        )
        problem: str | None = None
        if suite is None:
            problem = (
                "FORGE reported success without an acceptance suite; "
                "a success claim is not verification evidence"
            )
        elif not criteria:
            problem = "no acceptance criteria are defined for this work"
        try:
            workspace = safe_workspace(_workspace_from(payload.evidence, outputs))
        except UnsafeSuite as error:
            workspace, problem = None, problem or str(error)
        return VerificationSpec(
            source_message_id=message.message_id,
            suite=suite or "(no acceptance suite declared)",
            acceptance_criteria=criteria,
            task_id=payload.task_id,
            capability_id=capability_id,
            requirement_id=requirement_id,
            objective_id=message.objective_id or (task.objective_id if task else None),
            execution_run_ids=tuple(payload.execution_run_ids),
            criterion_tests=explicit_criterion_tests(payload.evidence),
            workspace=workspace,
            correlation_id=message.correlation_id,
            problem=problem,
        )

    async def _capability_for(self, ctx: AgentContext, requirement_id: UUID | None) -> UUID | None:
        if requirement_id is None:
            return None
        statement = (
            select(CapabilityRecord.id)
            .where(CapabilityRecord.requirement_id == requirement_id)
            .order_by(CapabilityRecord.updated_at.desc())
            .limit(1)
        )
        async with ctx.sessions() as session:
            return await session.scalar(statement)

    async def _load_spec(self, run_id: UUID, ctx: AgentContext) -> VerificationSpec | None:
        """Rebuild a pending run's spec from its durable source message (survives restarts)."""
        async with ctx.sessions() as session:
            row = await session.get(MessageRecord, run_id)
        if row is None:
            return self._specs.get(run_id)
        message = _message_from_row(row)
        payload = validate_payload(message)
        if isinstance(payload, VerificationRequestPayload):
            return await self._spec_from_request(message, payload, ctx)
        if isinstance(payload, ExecutionResultPayload):
            return await self._spec_from_execution(message, payload, ctx)
        return None

    # -- verification ------------------------------------------------------------------------

    def _tool_args(self, spec: VerificationSpec) -> dict[str, Any]:
        args: dict[str, Any] = {"argv": pytest_argv(self._python, parse_suite(spec.suite))}
        if spec.workspace:
            args["cwd"] = spec.workspace
        return args

    async def verify(self, spec: VerificationSpec, ctx: AgentContext) -> TestRunRecord:
        """Run (or resume, or replay) the verification described by ``spec``."""
        store = TestRunStore(ctx.sessions)
        existing = await store.get(spec.run_id)
        if existing is not None and existing.status not in (RunStatus.PENDING, RunStatus.RUNNING):
            await self._after_finish(spec, existing, None, ctx, replay=True)
            return existing
        run = existing or await store.open_run(
            spec.run_id,
            suite=spec.suite,
            task_id=spec.task_id,
            capability_id=spec.capability_id,
            execution_run_id=spec.execution_run_id,
            criteria=spec.acceptance_criteria,
            now=ctx.clock(),
        )
        if spec.problem is not None:
            return await self._finish_inconclusive(spec, spec.problem, ctx)
        if not spec.acceptance_criteria:
            return await self._finish_inconclusive(spec, "no acceptance criteria", ctx)
        try:
            args = self._tool_args(spec)
        except UnsafeSuite as error:
            return await self._finish_inconclusive(spec, f"unsafe acceptance suite: {error}", ctx)
        invocation = await self.invoke_tool(
            RUN_PROCESS_TOOL, args, ctx, correlation_id=spec.correlation_id
        )
        if invocation.result is not None:
            result = invocation.result
            if result.status is TaskResultStatus.WAITING_FOR_APPROVAL:
                self._specs[spec.run_id] = spec
                ctx.logger.info(
                    "LEDGER verification %s waits for WARDEN review",
                    spec.run_id,
                    extra={"input_hash": result.outputs.get("input_hash")},
                )
                return run
            reason = f"verification run {result.status.value}: {result.error or result.summary}"
            return await self._finish_inconclusive(spec, reason, ctx)
        return await self._evaluate_process(spec, invocation.value, ctx)

    async def _evaluate_process(
        self, spec: VerificationSpec, process: Any, ctx: AgentContext
    ) -> TestRunRecord:
        output = process.stdout.content
        parsed = parse_pytest_output(output)
        criteria = evaluate_criteria(spec.acceptance_criteria, parsed.cases, spec.criterion_tests)
        verdict = decide_verdict(
            parsed, criteria, exit_code=process.exit_code, timed_out=process.timed_out
        )
        if process.stdout_truncated and verdict.verdict is VerificationVerdict.VERIFIED:
            verdict = Verdict(VerificationVerdict.INCONCLUSIVE, "pytest output was truncated")
        evidence = [
            f"pytest_execution_run:{process.run_id}",
            f"exit_code:{process.exit_code}",
            f"stdout:{process.stdout_path}",
            f"stdout_sha256:{process.stdout.content_hash}",
        ]
        evidence += [f"verified_execution_run:{run_id}" for run_id in spec.execution_run_ids]
        linked_run = spec.execution_run_id or process.run_id
        return await self._finish(
            spec,
            verdict,
            parsed,
            criteria,
            execution_run_id=linked_run,
            report_path=str(process.stdout_path),
            duration_seconds=process.duration_seconds,
            evidence=evidence,
            ctx=ctx,
        )

    async def _finish_inconclusive(
        self, spec: VerificationSpec, reason: str, ctx: AgentContext
    ) -> TestRunRecord:
        empty = ParsedRun(cases=(), counts=TestCounts(), summary_found=False)
        criteria = [
            CriterionResult(criterion=criterion, passed=False, tests=(), evidence=reason[:1000])
            for criterion in spec.acceptance_criteria
        ]
        verdict = Verdict(VerificationVerdict.INCONCLUSIVE, reason)
        return await self._finish(
            spec,
            verdict,
            empty,
            criteria,
            execution_run_id=spec.execution_run_id,
            report_path=None,
            duration_seconds=0.0,
            evidence=[f"inconclusive:{reason}"[:500]],
            ctx=ctx,
        )

    async def _finish(
        self,
        spec: VerificationSpec,
        verdict: Verdict,
        parsed: ParsedRun,
        criteria: Sequence[CriterionResult],
        *,
        execution_run_id: UUID | None,
        report_path: str | None,
        duration_seconds: float,
        evidence: list[str],
        ctx: AgentContext,
    ) -> TestRunRecord:
        store = TestRunStore(ctx.sessions)
        await store.finish(
            spec.run_id,
            verdict=verdict.verdict,
            counts=merged_counts(parsed),
            cases=parsed.cases,
            acceptance_results=[result.to_json() for result in criteria],
            execution_run_id=execution_run_id,
            report_path=report_path,
            evidence=evidence,
            correlation_id=spec.correlation_id,
            now=ctx.clock(),
        )
        self._specs.pop(spec.run_id, None)
        run = await store.get(spec.run_id)
        assert run is not None
        await self._after_finish(spec, run, verdict.reason, ctx, replay=False)
        return run

    async def _after_finish(
        self,
        spec: VerificationSpec,
        run: TestRunRecord,
        reason: str | None,
        ctx: AgentContext,
        *,
        replay: bool,
    ) -> None:
        verdict = verdict_for_status(run.status)
        if verdict is None:
            return
        if not replay or await self._is_latest_for_capability(run, ctx):
            await self._apply_gate(spec, run, verdict, ctx)
        await self._publish_report(spec, run, verdict, reason, ctx)
        if verdict is VerificationVerdict.FAILED:
            await self._publish_blockers(spec, run, ctx)
        await self._resume_verification_tasks(ctx)

    async def _is_latest_for_capability(self, run: TestRunRecord, ctx: AgentContext) -> bool:
        if run.capability_id is None:
            return True
        finished = await TestRunStore(ctx.sessions).finished_for_capability(run.capability_id)
        return bool(finished) and finished[-1].id == run.id

    # -- security decisions ------------------------------------------------------------------

    async def _on_security_decision(
        self, payload: SecurityDecisionPayload, ctx: AgentContext
    ) -> None:
        """Resume (approved) or close (rejected / needs evidence) runs bound to this hash."""
        if ctx.tools is None:
            return
        for run in await TestRunStore(ctx.sessions).open_runs():
            spec = await self._load_spec(run.id, ctx)
            if spec is None or spec.problem is not None:
                continue
            try:
                operation = ctx.tools.build_operation(RUN_PROCESS_TOOL, self._tool_args(spec))
            except (UnsafeSuite, ValueError):
                continue
            if operation.input_hash != payload.input_hash:
                continue
            if payload.decision is DecisionOutcome.APPROVED:
                await self.verify(spec, ctx)
            else:
                reason = f"WARDEN {payload.decision.value}: {payload.rationale}"
                await self._finish_inconclusive(spec, reason[:1000], ctx)

    # -- execution results -------------------------------------------------------------------

    async def _on_execution_result(
        self, message: AgentMessage, payload: ExecutionResultPayload, ctx: AgentContext
    ) -> None:
        if payload.status is RunStatus.SUCCEEDED:
            if payload.outputs.get("verification_requested") is True:
                # FORGE follows up with a dedicated verification.request naming the suite;
                # verifying the bare success claim here would only duplicate that run.
                capability_id = _as_uuid(
                    payload.outputs.get("capability_id")
                ) or await self._capability_for(
                    ctx, payload.requirement_id or message.requirement_id
                )
                await self._mark_installed(ctx, capability_id, payload.execution_run_ids)
                ctx.logger.info(
                    "LEDGER recorded execution %s (verification requested separately)",
                    message.message_id,
                )
                return
            spec = await self._spec_from_execution(message, payload, ctx)
            if spec is None:
                ctx.logger.info(
                    "LEDGER recorded execution %s (nothing to verify)", message.message_id
                )
                return
            await self.verify(spec, ctx)
            return
        if payload.status is RunStatus.FAILED:
            await self._record_error_once(
                ctx,
                error_type="execution_failed",
                message=(payload.error or "execution failed")[:2000],
                aggregate_type="task",
                aggregate_id=payload.task_id,
                details={
                    "message_id": str(message.message_id),
                    "execution_run_ids": [str(run_id) for run_id in payload.execution_run_ids],
                },
                dedupe_key=("message_id", str(message.message_id)),
                task_id=payload.task_id,
                objective_id=message.objective_id,
                correlation_id=message.correlation_id,
            )

    # -- capability registration gate --------------------------------------------------------

    async def _apply_gate(
        self,
        spec: VerificationSpec,
        run: TestRunRecord,
        verdict: VerificationVerdict,
        ctx: AgentContext,
    ) -> None:
        if verdict is VerificationVerdict.VERIFIED:
            await self._on_verified(spec, run, ctx)
        elif verdict is VerificationVerdict.FAILED:
            await self._on_failed(spec, run, ctx)

    async def _on_verified(
        self, spec: VerificationSpec, run: TestRunRecord, ctx: AgentContext
    ) -> None:
        details = {"test_run_id": str(run.id), "execution_run_id": _text(run.execution_run_id)}
        capability = await self._capability(ctx, run.capability_id)
        verified = capability is None
        if capability is not None:
            repository = CapabilityRepository(ctx.sessions)
            status = CapabilityStatus(capability.lifecycle_status)
            if status is CapabilityStatus.INSTALLED:
                await repository.transition(
                    capability.id, CapabilityStatus.TESTED, actor=_ACTOR, details=details
                )
                status = CapabilityStatus.TESTED
            if status is CapabilityStatus.TESTED:
                await repository.transition(
                    capability.id, CapabilityStatus.VERIFIED, actor=_ACTOR, details=details
                )
                status = CapabilityStatus.VERIFIED
            verified = status is CapabilityStatus.VERIFIED
            if not verified:
                ctx.logger.warning(
                    "LEDGER did not register capability %s: it is %s, not installed",
                    capability.id,
                    status.value,
                )
            else:
                await self._resolve_verification_errors(capability.id, run, ctx)
                await self._publish_registration(spec, run, ctx)
        requirement_id = spec.requirement_id or (capability.requirement_id if capability else None)
        if verified and requirement_id is not None:
            await self._set_requirement(
                ctx,
                requirement_id,
                RequirementStatus.SATISFIED,
                allowed_from=(RequirementStatus.APPROVED, RequirementStatus.IN_PROGRESS),
                reason=f"verified by test run {run.id}",
            )

    async def _on_failed(
        self, spec: VerificationSpec, run: TestRunRecord, ctx: AgentContext
    ) -> None:
        capability = await self._capability(ctx, run.capability_id)
        regression = False
        if capability is not None:
            status = CapabilityStatus(capability.lifecycle_status)
            regression = status is CapabilityStatus.VERIFIED or await self._had_success(
                capability.id, run.id, ctx
            )
            if status in (CapabilityStatus.TESTED, CapabilityStatus.VERIFIED):
                await CapabilityRepository(ctx.sessions).transition(
                    capability.id,
                    CapabilityStatus.INSTALLED,
                    actor=_ACTOR,
                    details={"test_run_id": str(run.id), "reason": "acceptance tests failed"},
                )
        failing_criteria = [
            item["criterion"] for item in run.acceptance_results if not item.get("passed")
        ]
        results = await TestRunStore(ctx.sessions).results(run.id)
        broken = failing_tests(
            TestCaseResult(name=item.name, outcome=ResultOutcome(item.outcome)) for item in results
        )
        error_type = "verification_regression" if regression else "verification_failed"
        subject = f"capability {capability.name}" if capability else f"suite {run.suite}"
        await self._record_error_once(
            ctx,
            error_type=error_type,
            message=(
                f"{'Regression' if regression else 'Verification failed'} for {subject}: "
                f"{len(broken)} failing test(s)"
            ),
            aggregate_type="capability" if capability else "test_run",
            aggregate_id=capability.id if capability else run.id,
            details={
                "test_run_id": str(run.id),
                "execution_run_id": _text(run.execution_run_id),
                "failing_criteria": failing_criteria[:20],
                "failing_tests": broken[:20],
                "suite": run.suite,
            },
            dedupe_key=("test_run_id", str(run.id)),
            task_id=spec.task_id,
            objective_id=spec.objective_id,
            correlation_id=spec.correlation_id,
        )
        requirement_id = spec.requirement_id or (capability.requirement_id if capability else None)
        if regression and requirement_id is not None:
            await self._set_requirement(
                ctx,
                requirement_id,
                RequirementStatus.IN_PROGRESS,
                allowed_from=(RequirementStatus.SATISFIED,),
                reason=f"regression found by test run {run.id}",
            )

    async def _had_success(self, capability_id: UUID, run_id: UUID, ctx: AgentContext) -> bool:
        finished = await TestRunStore(ctx.sessions).finished_for_capability(capability_id)
        return any(
            item.id != run_id and item.status == RunStatus.SUCCEEDED.value for item in finished
        )

    async def _resolve_verification_errors(
        self, capability_id: UUID, run: TestRunRecord, ctx: AgentContext
    ) -> None:
        """Link the retest to every open failure/regression of this capability and close it."""
        errors = ErrorRepository(ctx.sessions)
        statement = select(ErrorRecord).where(
            ErrorRecord.resolved.is_(False),
            ErrorRecord.aggregate_id == capability_id,
            ErrorRecord.error_type.in_(VERIFICATION_ERROR_TYPES),
        )
        async with ctx.sessions() as session:
            open_errors = list(await session.scalars(statement))
        for error in open_errors:
            await errors.record_recovery_attempt(
                error.id,
                action="retest",
                outcome="verified",
                actor=_ACTOR,
                details={
                    "test_run_id": str(run.id),
                    "execution_run_id": _text(run.execution_run_id),
                    "failed_test_run_id": error.details.get("test_run_id"),
                    "failed_execution_run_id": error.details.get("execution_run_id"),
                },
                resolves=True,
            )

    async def _capability(
        self, ctx: AgentContext, capability_id: UUID | None
    ) -> CapabilityRecord | None:
        if capability_id is None:
            return None
        async with ctx.sessions() as session:
            return await session.get(CapabilityRecord, capability_id)

    async def _mark_installed(
        self, ctx: AgentContext, capability_id: UUID | None, run_ids: Sequence[UUID]
    ) -> None:
        """An approved capability FORGE built (recorded execution runs) is installed, unverified."""
        capability = await self._capability(ctx, capability_id)
        if (
            capability is None
            or not run_ids
            or CapabilityStatus(capability.lifecycle_status) is not CapabilityStatus.APPROVED
        ):
            return
        await CapabilityRepository(ctx.sessions).transition(
            capability.id,
            CapabilityStatus.INSTALLED,
            actor=_ACTOR,
            details={"execution_run_ids": [str(run_id) for run_id in run_ids]},
        )

    async def _approve_capabilities(
        self, ctx: AgentContext, requirement_id: UUID, reason: str
    ) -> None:
        """Approving a requirement approves the capability proposed to satisfy it."""
        async with ctx.sessions() as session:
            requirement = await session.get(RequirementRecord, requirement_id)
            if requirement is None or requirement.status not in (
                RequirementStatus.APPROVED.value,
                RequirementStatus.IN_PROGRESS.value,
            ):
                return
        statement = select(CapabilityRecord.id).where(
            CapabilityRecord.requirement_id == requirement_id,
            CapabilityRecord.lifecycle_status == CapabilityStatus.PROPOSED.value,
        )
        async with ctx.sessions() as session:
            capability_ids = list(await session.scalars(statement))
        for capability_id in capability_ids:
            await CapabilityRepository(ctx.sessions).transition(
                capability_id,
                CapabilityStatus.APPROVED,
                actor=_ACTOR,
                details={"requirement_id": str(requirement_id), "reason": reason[:500]},
            )

    async def _set_requirement(
        self,
        ctx: AgentContext,
        requirement_id: UUID,
        status: RequirementStatus,
        *,
        allowed_from: Sequence[RequirementStatus],
        reason: str,
    ) -> bool:
        async with ctx.sessions() as session:
            requirement = await session.get(RequirementRecord, requirement_id)
        if requirement is None or RequirementStatus(requirement.status) not in allowed_from:
            return False
        await RequirementRepository(ctx.sessions).set_status(
            requirement_id, status, actor=_ACTOR, reason=reason
        )
        return True

    async def _record_error_once(
        self,
        ctx: AgentContext,
        *,
        error_type: str,
        message: str,
        aggregate_type: str,
        aggregate_id: UUID,
        details: dict[str, Any],
        dedupe_key: tuple[str, str],
        task_id: UUID | None,
        objective_id: UUID | None,
        correlation_id: UUID | None,
    ) -> UUID:
        statement = select(ErrorRecord).where(
            ErrorRecord.error_type == error_type, ErrorRecord.aggregate_id == aggregate_id
        )
        async with ctx.sessions() as session:
            for existing in await session.scalars(statement):
                if existing.details.get(dedupe_key[0]) == dedupe_key[1]:
                    return existing.id
        return await ErrorRepository(ctx.sessions).record(
            error_type=error_type,
            message=message,
            agent=_ACTOR,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            details=details,
            objective_id=objective_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )

    # -- outbound messages -------------------------------------------------------------------

    async def _envelope(
        self, spec: VerificationSpec, ctx: AgentContext, **ids: UUID | None
    ) -> dict[str, Any]:
        """Envelope ids that exist (message rows carry real foreign keys)."""
        wanted = {
            "objective_id": ids.get("objective_id", spec.objective_id),
            "task_id": ids.get("task_id", spec.task_id),
            "requirement_id": ids.get("requirement_id", spec.requirement_id),
        }
        models = {"objective_id": ObjectiveRecord, "requirement_id": RequirementRecord}
        envelope: dict[str, Any] = {}
        async with ctx.sessions() as session:
            for name, value in wanted.items():
                if name == "task_id":
                    exists = value is not None and await task_ref_in(session, value) is not None
                else:
                    exists = await existing_id(session, models[name], value) is not None
                if exists:
                    envelope[name] = value
        if spec.correlation_id is not None:
            envelope["correlation_id"] = spec.correlation_id
        return envelope

    async def _send(
        self,
        target: AgentId,
        payload: Payload,
        spec: VerificationSpec,
        ctx: AgentContext,
        *,
        key: str,
        **ids: UUID | None,
    ) -> None:
        envelope = await self._envelope(spec, ctx, **ids)
        message = build_message(
            self.agent_id,
            target,
            payload,
            message_id=_derived_id(spec.run_id, key, target.value),
            **envelope,
        )
        await ctx.bus.publish(message)

    async def _publish_report(
        self,
        spec: VerificationSpec,
        run: TestRunRecord,
        verdict: VerificationVerdict,
        reason: str | None,
        ctx: AgentContext,
    ) -> None:
        results = await TestRunStore(ctx.sessions).results(run.id)
        cases = [
            TestCaseResult(
                name=item.name,
                outcome=ResultOutcome(item.outcome),
                duration_ms=item.duration_ms,
                message=item.message,
            )
            for item in results
        ]
        skipped = sum(1 for case in cases if case.outcome is ResultOutcome.SKIPPED)
        counts = TestCounts(
            passed=run.passed, failed=run.failed, errors=run.errors, skipped=skipped
        )
        acceptance = [
            AcceptanceResult(
                criterion=item["criterion"],
                passed=bool(item.get("passed")),
                evidence=str(item.get("evidence", "")),
            )
            for item in run.acceptance_results
            if str(item.get("criterion", "")).strip()
        ]
        summary = reason or (
            f"{verdict.value}: {counts.passed} passed, {counts.failed} failed, "
            f"{counts.errors} error(s), {counts.skipped} skipped"
        )
        payload = TestReportPayload(
            test_run_id=run.id,
            task_id=spec.task_id,
            capability_id=run.capability_id,
            requirement_id=spec.requirement_id,
            suite=run.suite,
            results=counts,
            cases=cases,
            acceptance_results=acceptance,
            verdict=verdict,
            summary=summary[:2000],
        )
        targets = [AgentId.ORCHESTR]
        if verdict is VerificationVerdict.FAILED:
            targets.append(AgentId.FORGE)
        for target in targets:
            await self._send(target, payload, spec, ctx, key="test.report")

    async def _publish_registration(
        self, spec: VerificationSpec, run: TestRunRecord, ctx: AgentContext
    ) -> None:
        capability = await self._capability(ctx, run.capability_id)
        if capability is None or capability.lifecycle_status != CapabilityStatus.VERIFIED:
            return
        payload = CapabilityRegisterPayload(
            capability_id=capability.id,
            name=capability.name,
            description=capability.description,
            lifecycle_status=CapabilityStatus.VERIFIED,
            provider=capability.provider,
            version=capability.version,
            entrypoint=capability.entrypoint,
            input_schema=capability.input_schema,
            output_schema=capability.output_schema,
            required_permissions=list(capability.required_permissions),
            dependencies=list(capability.dependencies),
            verification_status=CapabilityVerification.VERIFIED,
            verification_evidence=[
                {
                    "test_run_id": str(run.id),
                    "suite": run.suite,
                    "execution_run_id": _text(run.execution_run_id),
                    "report_path": run.report_path,
                    "acceptance_results": run.acceptance_results,
                }
            ],
            test_run_ids=[run.id],
            known_limitations=list(capability.known_limitations),
            requirement_id=capability.requirement_id,
            objective_id=capability.objective_id,
        )
        for target in (AgentId.ORCHESTR, AgentId.SCOUT):
            await self._send(
                target,
                payload,
                spec,
                ctx,
                key="capability.register",
                objective_id=capability.objective_id,
                requirement_id=capability.requirement_id,
            )

    async def _publish_blockers(
        self, spec: VerificationSpec, run: TestRunRecord, ctx: AgentContext
    ) -> None:
        if spec.task_id is None:
            return
        if await self.work_queue(ctx).task_ref(spec.task_id) is None:
            return
        failing = [item["criterion"] for item in run.acceptance_results if not item.get("passed")]
        evidence = [{"test_run_id": str(run.id), "failing_criteria": failing[:20]}]
        attempts = await self._consecutive_failures(run, ctx)
        implementation = BlockerPayload(
            blocker_id=_derived_id(run.id, "blocker", BlockerKind.IMPLEMENTATION.value),
            task_id=spec.task_id,
            objective_id=spec.objective_id,
            kind=BlockerKind.IMPLEMENTATION,
            description=f"Acceptance tests failed for {run.suite}; fix and request a retest.",
            route_to=AgentId.FORGE,
            attempts=attempts,
            evidence=evidence,
        )
        await self._send(AgentId.FORGE, implementation, spec, ctx, key="blocker.implementation")
        if attempts >= self._research_escalation_after:
            research = BlockerPayload(
                blocker_id=_derived_id(run.id, "blocker", BlockerKind.RESEARCH.value),
                task_id=spec.task_id,
                objective_id=spec.objective_id,
                kind=BlockerKind.RESEARCH,
                description=(
                    f"Acceptance tests for {run.suite} failed {attempts} times in a row; "
                    "the approach may need research."
                ),
                route_to=AgentId.SCOUT,
                attempts=attempts,
                evidence=evidence,
            )
            await self._send(AgentId.SCOUT, research, spec, ctx, key="blocker.research")

    async def _consecutive_failures(self, run: TestRunRecord, ctx: AgentContext) -> int:
        if run.capability_id is None:
            return 1
        finished = await TestRunStore(ctx.sessions).finished_for_capability(run.capability_id)
        streak = 0
        for item in reversed(finished):
            if item.status in (RunStatus.CANCELLED.value, RunStatus.INCONCLUSIVE.value):
                continue
            if item.status != RunStatus.FAILED.value:
                break
            streak += 1
        return max(streak, 1)

    # -- requirements ledger -----------------------------------------------------------------

    async def _on_requirement_submit(
        self, message: AgentMessage, payload: RequirementSubmitPayload, ctx: AgentContext
    ) -> None:
        requirement_id = payload.requirement_id or _derived_id(message.message_id, "requirement")
        async with ctx.sessions() as session, session.begin():
            if await session.get(RequirementRecord, requirement_id) is not None:
                return
            if await session.get(ObjectiveRecord, payload.objective_id) is None:
                raise LookupError(f"objective {payload.objective_id} does not exist")
            if payload.requirement_id is None and await self._duplicate_requirement(
                session, payload
            ):
                ctx.logger.info("LEDGER ignored a duplicate requirement submission")
                return
            await self._stage_requirement(
                session,
                requirement_id=requirement_id,
                objective_id=payload.objective_id,
                title=payload.title,
                description=payload.description,
                requirement_type=payload.requirement_type.value,
                acceptance_criteria=list(payload.acceptance_criteria),
                task_id=payload.task_id,
                source_finding_id=payload.source_finding_id,
                priority=payload.priority,
                security_review_required=payload.security_review_required,
                created_by=message.source_agent.value,
                correlation_id=message.correlation_id,
            )

    @staticmethod
    async def _duplicate_requirement(
        session: AsyncSession, payload: RequirementSubmitPayload
    ) -> bool:
        statement = select(RequirementRecord.id).where(
            RequirementRecord.objective_id == payload.objective_id,
            RequirementRecord.title == payload.title,
            RequirementRecord.requirement_type == payload.requirement_type.value,
            RequirementRecord.status.in_(OPEN_REQUIREMENT_STATES),
        )
        return await session.scalar(statement.limit(1)) is not None

    @staticmethod
    async def _stage_requirement(
        session: AsyncSession,
        *,
        requirement_id: UUID,
        objective_id: UUID,
        title: str,
        description: str,
        requirement_type: str,
        acceptance_criteria: list[str],
        task_id: UUID | None,
        source_finding_id: UUID | None,
        priority: int,
        security_review_required: bool,
        created_by: str,
        correlation_id: UUID | None,
    ) -> None:
        # RequirementRepository.create cannot take a caller id; the ledger needs the submitted
        # (or a message-derived) id so redelivery and cross-agent references stay stable.
        linked_task = await task_ref_in(session, task_id) if task_id is not None else None
        record = RequirementRecord(
            id=requirement_id,
            objective_id=objective_id,
            task_id=linked_task.id if linked_task else None,
            title=title,
            description=description,
            requirement_type=requirement_type,
            status=RequirementStatus.PROPOSED.value,
            acceptance_criteria=acceptance_criteria,
            source_finding_id=await existing_id(session, ResearchFindingRecord, source_finding_id),
            priority=priority,
            security_review_required=security_review_required,
            created_by=created_by,
        )
        session.add(record)
        await session.flush()
        add_event(
            session,
            event_type="requirement.created",
            aggregate_type="requirement",
            aggregate_id=requirement_id,
            actor=_ACTOR,
            payload={
                "title": title,
                "requirement_type": requirement_type,
                "source_finding_id": record.source_finding_id,
                "security_review_required": security_review_required,
            },
            category=EventCategory.REQUIREMENT,
            correlation_id=correlation_id,
            objective_id=objective_id,
            task_id=record.task_id,
            idempotency_key=f"requirement.created:{requirement_id}",
            source="agents.ledger",
        )

    async def _on_requirement_evaluated(
        self, payload: RequirementEvaluatedPayload, ctx: AgentContext
    ) -> None:
        async with ctx.sessions() as session, session.begin():
            requirement = await session.get(RequirementRecord, payload.requirement_id)
            if requirement is None:
                if await session.get(ObjectiveRecord, payload.objective_id) is None:
                    raise LookupError(f"objective {payload.objective_id} does not exist")
                await self._stage_requirement(
                    session,
                    requirement_id=payload.requirement_id,
                    objective_id=payload.objective_id,
                    title=payload.title,
                    description="",
                    requirement_type=payload.requirement_type.value,
                    acceptance_criteria=list(payload.acceptance_criteria),
                    task_id=None,
                    source_finding_id=payload.source_finding_id,
                    priority=payload.priority if payload.priority is not None else 50,
                    security_review_required=payload.security_review_required,
                    created_by=AgentId.SCOUT.value,
                    correlation_id=None,
                )
            elif not requirement.acceptance_criteria and payload.acceptance_criteria:
                requirement.acceptance_criteria = list(payload.acceptance_criteria)
        if payload.outcome is EvaluationOutcome.APPROVED:
            await self._set_requirement(
                ctx,
                payload.requirement_id,
                RequirementStatus.APPROVED,
                allowed_from=(RequirementStatus.PROPOSED,),
                reason=payload.rationale,
            )
            await self._approve_capabilities(ctx, payload.requirement_id, payload.rationale)
        elif payload.outcome is EvaluationOutcome.REJECTED:
            await self._set_requirement(
                ctx,
                payload.requirement_id,
                RequirementStatus.REJECTED,
                allowed_from=(RequirementStatus.PROPOSED, RequirementStatus.APPROVED),
                reason=payload.rationale,
            )


def _text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None
