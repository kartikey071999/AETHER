"""WARDEN: deterministic security review, durable decisions, and the user-authorization gate.

Every outcome is decided by the rules in ``workflow.py``: allowlists, sensitive paths, the
declared requirement, provenance (source, integrity hash, version pin) and least-privilege
restrictions. Evidence attached to a request is untrusted data: it is quoted, scanned for
instruction-like text and recorded, but it can never turn a rejection into an approval. An
optional reasoning-tier model call summarizes risks for humans; its output is advisory only.

Decisions are recorded through ``ctx.services["approvals"]`` (only WARDEN is given it) and bind
to the operation's ``input_hash``. Waiting tasks are resumed only by a live approval and, for
high-impact kinds, an explicit approving ``user.response``; silence and expiry never authorize.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.warden.prompts import RISK_SUMMARY_SYSTEM, risk_summary_prompt
from aether.agents.warden.schemas import RiskSummary
from aether.agents.warden.workflow import Assessment, RequirementFacts, evaluate
from aether.agents.work_queue import task_ref_in
from aether.communication.contracts import (
    AuthorizationRequestPayload,
    InvalidPayload,
    Payload,
    SecurityDecisionPayload,
    SecurityEvaluatePayload,
    UserDecision,
    UserResponsePayload,
    build_message,
    check_route,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.models.contracts import CompletionMessage, CompletionRequest, ModelTier
from aether.models.facade import ModelCallContext
from aether.security import DecisionOutcome, SecurityDecision
from aether.security.operations import Operation
from aether.security.policy import SecurityPolicy
from aether.security.untrusted import UntrustedContent
from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    EventRecord,
    ObjectiveRecord,
    RequirementRecord,
    RequirementStatus,
    SecurityDecisionRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import TaskRepository
from aether.storage.repositories.authorization import (
    AuthorizationRequestRepository,
    InvalidAuthorizationResponse,
)
from aether.storage.repositories.security import OperationAuthorizationRequests
from aether.storage.repositories.tasks import InvalidTaskTransition

# Deterministic ids for everything WARDEN publishes, so redelivery is idempotent.
WARDEN_NAMESPACE = UUID("8c4b2e7a-1f3d-4a69-b5e0-93d7c6a1f254")
_ACTOR = AgentId.WARDEN.value
_USER_ACTOR = "user"
_EVIDENCE_ITEMS = 20
_EVIDENCE_EXCERPT = 500
_RATIONALE_LIMIT = 2000


@runtime_checkable
class DecisionRecorder(Protocol):
    """The injected approvals service (agents never import it directly)."""

    async def record_decision(
        self,
        decided_by: AgentId | str,
        operation: Operation,
        decision: DecisionOutcome | str,
        *,
        rationale: str,
        evidence: Sequence[Mapping[str, Any] | UntrustedContent] = (),
        scope: Mapping[str, Any] | None = None,
        restrictions: Sequence[str] = (),
        risks: Sequence[str] = (),
        expires_at: datetime | None = None,
        version_constraint: str | None = None,
        correlation_id: UUID | None = None,
    ) -> SecurityDecision: ...


def _derived_id(*parts: object) -> UUID:
    return uuid5(WARDEN_NAMESPACE, ":".join(str(part) for part in parts))


def _utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive timestamps; they are stored as UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _existing(session: AsyncSession, model: type[Any], value: UUID | None) -> UUID | None:
    if value is None:
        return None
    return value if await session.get(model, value) is not None else None


@dataclass(slots=True)
class SweepResult:
    """Tasks the resume sweep moved out of ``waiting_for_approval``."""

    resumed: list[UUID] = field(default_factory=list)
    blocked: list[UUID] = field(default_factory=list)


class WardenAgent(BaseAgent):
    """Security reviewer. The decision rule is code; the model only summarizes risks."""

    agent_id = AgentId.WARDEN
    guard_terminal_objectives = True
    accepted_message_types = receivable_types(AgentId.WARDEN)

    def __init__(self, *, summarize_risks: bool = True) -> None:
        super().__init__()
        self._summarize_risks = summarize_risks

    # -- message handling --------------------------------------------------------------------

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        if message.target_agent is not self.agent_id:
            raise ValueError(f"message addressed to {message.target_agent.value}, not WARDEN")
        await self.resume_waiting_tasks(ctx)
        if message.message_type is MessageType.SECURITY_EVALUATE:
            await self._on_security_evaluate(message, ctx)
            return
        payload = validate_message(message)
        if isinstance(payload, UserResponsePayload):
            await self._on_user_response(message, payload, ctx)
        else:
            ctx.logger.info(
                "WARDEN noted %s from %s",
                message.message_type.value,
                message.source_agent.value,
                extra={"message_id": str(message.message_id)},
            )

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        """WARDEN's scheduled work is the resume sweep over waiting tasks."""
        result = await self.resume_waiting_tasks(ctx)
        return TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary=(f"resume sweep: {len(result.resumed)} resumed, {len(result.blocked)} blocked"),
            outputs={
                "resumed": [str(item) for item in result.resumed],
                "blocked": [str(item) for item in result.blocked],
            },
        )

    # -- security.evaluate -------------------------------------------------------------------

    async def review(
        self,
        operation: Operation,
        ctx: AgentContext,
        *,
        evidence: Sequence[Mapping[str, Any]] = (),
        review_message_id: UUID | None = None,
        correlation_id: UUID | None = None,
        block_reason: str | None = None,
    ) -> SecurityDecision:
        """Evaluate ``operation`` and durably record the decision (no messages are sent)."""
        approvals, policy = self._security(ctx)
        if review_message_id is not None:
            existing = await self._decision_for_review(ctx, operation, review_message_id)
            if existing is not None:
                return existing
        requirement = await self._requirement(ctx, operation)
        assessment = evaluate(operation, policy, requirement=requirement, evidence=evidence)
        summary = await self._risk_summary(operation, assessment, evidence, ctx)
        risks = list(assessment.risks)
        if summary is not None:
            risks.extend(summary.advisory_risks())
        return await approvals.record_decision(
            AgentId.WARDEN,
            await self._storable(operation, ctx),
            assessment.outcome,
            rationale=assessment.rationale[:_RATIONALE_LIMIT],
            evidence=self._evidence(operation, assessment, evidence, summary),
            scope=self._scope(assessment, review_message_id, block_reason),
            restrictions=assessment.restrictions,
            risks=risks,
            version_constraint=assessment.version_constraint,
            correlation_id=correlation_id,
        )

    async def _on_security_evaluate(self, message: AgentMessage, ctx: AgentContext) -> None:
        check_route(message)
        try:
            payload = validate_message(message)
        except InvalidPayload as error:
            await self._reject_invalid_request(message, error, ctx)
            return
        assert isinstance(payload, SecurityEvaluatePayload)
        decision = await self.review(
            payload.operation,
            ctx,
            evidence=payload.evidence,
            review_message_id=message.message_id,
            correlation_id=message.correlation_id,
            block_reason=payload.block_reason.value if payload.block_reason else None,
        )
        await self._after_decision(payload.operation, decision, message, ctx)

    async def _reject_invalid_request(
        self, message: AgentMessage, error: InvalidPayload, ctx: AgentContext
    ) -> None:
        """Bind a rejection to the real hash of a request whose claimed hash is wrong."""
        raw = message.payload.get("operation") if isinstance(message.payload, Mapping) else None
        try:
            operation = Operation.model_validate(raw)
        except ValidationError:
            raise error from None
        if (
            operation.requested_by is AgentId.WARDEN
            or operation.requested_by is not message.source_agent
        ):
            # Nobody may obtain a decision for another agent's (or WARDEN's own) operation.
            raise error
        approvals, _ = self._security(ctx)
        existing = await self._decision_for_review(ctx, operation, message.message_id)
        decision = existing or await approvals.record_decision(
            AgentId.WARDEN,
            await self._storable(operation, ctx),
            DecisionOutcome.REJECTED,
            rationale=f"rejected: request_integrity: {error.detail}"[:_RATIONALE_LIMIT],
            evidence=[
                {
                    "type": "warden_check",
                    "check": "request_integrity",
                    "passed": False,
                    "detail": "the claimed input_hash or ids do not match the operation",
                    "claimed_input_hash": str(message.payload.get("input_hash", ""))[:128],
                    "actual_input_hash": operation.input_hash,
                }
            ],
            scope={"review_message_id": str(message.message_id), "invalid_request": True},
            correlation_id=message.correlation_id,
        )
        await self._after_decision(operation, decision, message, ctx)

    async def _after_decision(
        self,
        operation: Operation,
        decision: SecurityDecision,
        message: AgentMessage,
        ctx: AgentContext,
    ) -> None:
        payload = SecurityDecisionPayload(
            decision_id=decision.decision_id,
            decision=decision.decision,
            input_hash=decision.input_hash,
            operation=decision.operation,
            scope=decision.scope,
            evidence=decision.evidence,
            restrictions=decision.restrictions,
            risks=decision.risks,
            version_constraint=decision.version_constraint,
            expires_at=decision.expires_at,
            rationale=decision.rationale,
        )
        targets = [operation.requested_by]
        if operation.requested_by is not AgentId.ORCHESTR:
            targets.append(AgentId.ORCHESTR)
        for target in targets:
            await self._send(
                target,
                payload,
                ctx,
                key=f"{decision.decision_id}:security.decision",
                operation=operation,
                correlation_id=message.correlation_id,
            )
        needs_user = bool(decision.scope.get("needs_user_authorization"))
        if decision.decision is DecisionOutcome.APPROVED and needs_user:
            await self._request_user_authorization(operation, decision, message, ctx)
            return
        await self._settle(operation.task_id, decision.decision_id, ctx)

    # -- user authorization ------------------------------------------------------------------

    async def _request_user_authorization(
        self,
        operation: Operation,
        decision: SecurityDecision,
        message: AgentMessage,
        ctx: AgentContext,
    ) -> UUID:
        question = (
            f"{operation.requested_by.value} requests {operation.describe()} "
            f"(input sha256 {operation.input_hash[:16]}). WARDEN cleared it under policy; "
            "it will not run unless you explicitly approve. Authorize?"
        )
        storable = await self._storable(operation, ctx)
        async with ctx.sessions() as session:
            current = await OperationAuthorizationRequests.latest_for_decision(
                session, decision.decision_id
            )
        if current is not None:
            request_id = current.id
        else:
            request_id = await AuthorizationRequestRepository(ctx.sessions).create(
                question=question,
                actor=_ACTOR,
                objective_id=storable.objective_id,
                task_id=storable.task_id,
                security_decision_id=decision.decision_id,
                expires_at=decision.expires_at,
                correlation_id=message.correlation_id,
                input_hash=operation.input_hash,
            )
        payload = AuthorizationRequestPayload(
            authorization_request_id=request_id,
            question=question,
            operation_summary=operation.describe(),
            expires_at=decision.expires_at,
            security_decision_id=decision.decision_id,
            input_hash=operation.input_hash,
            objective_id=storable.objective_id,
            task_id=storable.task_id,
        )
        await self._send(
            AgentId.MUSE,
            payload,
            ctx,
            key=f"{decision.decision_id}:authorization.request",
            operation=operation,
            correlation_id=message.correlation_id,
        )
        return request_id

    async def _on_user_response(
        self, message: AgentMessage, payload: UserResponsePayload, ctx: AgentContext
    ) -> None:
        if payload.decision is UserDecision.NONE or payload.authorization_request_id is None:
            # A reply without an explicit decision is never authorization.
            ctx.logger.info(
                "WARDEN ignored a user response without an explicit decision",
                extra={"message_id": str(message.message_id)},
            )
            return
        request_id = payload.authorization_request_id
        async with ctx.sessions() as session:
            request = await session.get(AuthorizationRequestRecord, request_id)
        if request is None:
            ctx.logger.warning(
                "user response references an unknown authorization request",
                extra={"authorization_request_id": str(request_id)},
            )
            return
        if payload.task_id is not None and request.task_id not in (None, payload.task_id):
            ctx.logger.warning(
                "user response task does not match the authorization request",
                extra={"authorization_request_id": str(request_id)},
            )
            return
        try:
            await AuthorizationRequestRepository(ctx.sessions).respond(
                request_id,
                approved=payload.grants_authorization,
                actor=_USER_ACTOR,
                response=payload.text[:2000] or payload.decision.value,
                now=ctx.clock(),
                correlation_id=message.correlation_id,
            )
        except InvalidAuthorizationResponse:
            ctx.logger.info(
                "authorization request was already answered",
                extra={"authorization_request_id": str(request_id)},
            )
        if request.security_decision_id is not None:
            await self._settle(request.task_id, request.security_decision_id, ctx)

    # -- resuming waiting tasks --------------------------------------------------------------

    async def resume_waiting_tasks(self, ctx: AgentContext) -> SweepResult:
        """Resume or block every waiting task whose latest WARDEN decision settles it.

        This catches decisions made while the requester was still running (so the task was not
        yet waiting) and survives restarts: all state is read from the database.
        """
        result = SweepResult()
        if "approvals" not in ctx.services:
            return result
        await AuthorizationRequestRepository(ctx.sessions).expire_overdue(
            actor=_ACTOR, now=ctx.clock()
        )
        # Oldest first and bounded; a periodic sweep picks up any remainder next tick.
        waiting = [item.id for item in await self.work_queue(ctx).waiting_for_approval()]
        for task_id in waiting:
            async with ctx.sessions() as session:
                decision = await session.scalar(
                    select(SecurityDecisionRecord)
                    .where(SecurityDecisionRecord.task_id == task_id)
                    .order_by(SecurityDecisionRecord.created_at.desc())
                    .limit(1)
                )
            if decision is None:
                continue
            status = await self._settle(task_id, decision.id, ctx)
            if status is TaskStatus.READY:
                result.resumed.append(task_id)
            elif status is TaskStatus.BLOCKED:
                result.blocked.append(task_id)
        return result

    async def _settle(
        self, task_id: UUID | None, decision_id: UUID, ctx: AgentContext
    ) -> TaskStatus | None:
        """Move a waiting task according to ``decision_id``; None when nothing changes."""
        if task_id is None:
            return None
        async with ctx.sessions() as session:
            task = await task_ref_in(session, task_id)
            decision = await session.get(SecurityDecisionRecord, decision_id)
            if task is None or decision is None:
                return None
            if task.state != TaskStatus.WAITING_FOR_APPROVAL.value:
                return None
            if decision.scope.get("decided_by") != _ACTOR:
                return None
            if await self._consumed(session, task_id, decision_id):
                return None
            request = await OperationAuthorizationRequests.latest_for_decision(session, decision_id)
        target, reason = self._disposition(decision, request, ctx.clock())
        if target is None:
            return None
        details: dict[str, Any] = {
            "security_decision_id": str(decision_id),
            "decision": decision.decision,
            "reason": reason,
            "input_hash": decision.input_hash,
        }
        if request is not None:
            details["authorization_request_id"] = str(request.id)
        try:
            await TaskRepository(ctx.sessions).transition(
                task_id,
                target,
                actor=_ACTOR,
                details=details,
                error=reason if target is TaskStatus.BLOCKED else None,
            )
        except (InvalidTaskTransition, LookupError):
            return None
        ctx.logger.info(
            "WARDEN moved task %s to %s",
            task_id,
            target.value,
            extra={"security_decision_id": str(decision_id), "reason": reason},
        )
        return target

    @staticmethod
    def _disposition(
        decision: SecurityDecisionRecord,
        request: AuthorizationRequestRecord | None,
        now: datetime,
    ) -> tuple[TaskStatus | None, str]:
        if decision.decision != DecisionOutcome.APPROVED.value:
            return TaskStatus.BLOCKED, f"WARDEN decision: {decision.decision}"
        expires_at = _utc(decision.expires_at)
        if expires_at is None or expires_at <= now:
            return TaskStatus.BLOCKED, "the WARDEN approval expired before the task resumed"
        if not decision.scope.get("needs_user_authorization"):
            return TaskStatus.READY, "WARDEN approved the operation"
        if request is None:
            return None, "waiting for a user authorization request"
        request_expiry = _utc(request.expires_at)
        live = request_expiry is None or request_expiry > now
        if request.status == AuthorizationStatus.APPROVED.value and live:
            return TaskStatus.READY, "WARDEN approved and the user explicitly authorized"
        if request.status == AuthorizationStatus.DENIED.value:
            return TaskStatus.BLOCKED, "the user denied the operation"
        if request.status == AuthorizationStatus.PENDING.value and live:
            return None, "waiting for explicit user authorization"
        # Expired (or an approval that outlived its window): silence never authorizes.
        return TaskStatus.BLOCKED, "user authorization expired without an explicit approval"

    @staticmethod
    async def _consumed(session: AsyncSession, task_id: UUID, decision_id: UUID) -> bool:
        """Whether WARDEN already moved this task on account of this decision."""
        events = await session.scalars(
            select(EventRecord.payload).where(
                EventRecord.aggregate_type == "task",
                EventRecord.aggregate_id == task_id,
                EventRecord.event_type == "task.transitioned",
                EventRecord.actor == _ACTOR,
            )
        )
        wanted = str(decision_id)
        for payload in events:
            details = payload.get("details") if isinstance(payload, Mapping) else None
            if isinstance(details, Mapping) and details.get("security_decision_id") == wanted:
                return True
        return False

    # -- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _security(ctx: AgentContext) -> tuple[DecisionRecorder, SecurityPolicy]:
        approvals = ctx.services.get("approvals")
        if not isinstance(approvals, DecisionRecorder):
            raise TypeError("WARDEN requires the approvals service to record decisions")
        policy = ctx.services.get("security_policy")
        if not isinstance(policy, SecurityPolicy):
            # The approvals service is built with the process-wide immutable policy.
            policy = getattr(approvals, "_policy", None)
        if not isinstance(policy, SecurityPolicy):
            raise TypeError("WARDEN requires the security policy")
        return approvals, policy

    @staticmethod
    async def _decision_for_review(
        ctx: AgentContext, operation: Operation, review_message_id: UUID
    ) -> SecurityDecision | None:
        """The decision already recorded for this exact request message (redelivery)."""
        async with ctx.sessions() as session:
            rows = await session.scalars(
                select(SecurityDecisionRecord).where(
                    SecurityDecisionRecord.input_hash == operation.input_hash
                )
            )
            wanted = str(review_message_id)
            row = next((r for r in rows if r.scope.get("review_message_id") == wanted), None)
        if row is None:
            return None
        scope = dict(row.scope)
        return SecurityDecision(
            decision_id=row.id,
            decision=DecisionOutcome(row.decision),
            operation=row.operation,
            input_hash=row.input_hash,
            scope=scope,
            evidence=list(row.evidence),
            rationale=row.rationale,
            restrictions=tuple(scope.get("restrictions") or ()),
            risks=tuple(scope.get("risks") or ()),
            version_constraint=scope.get("version_constraint"),
            expires_at=_utc(row.expires_at),
            decided_at=_utc(row.created_at) or datetime.now(UTC),
        )

    @staticmethod
    async def _storable(operation: Operation, ctx: AgentContext) -> Operation:
        """Drop context ids that have no row (records carry real foreign keys).

        Task and objective ids are not part of the reviewed input, so the hash is unchanged.
        """
        async with ctx.sessions() as session:
            task = (
                None if operation.task_id is None else await task_ref_in(session, operation.task_id)
            )
            task_id = task.id if task else None
            objective_id = await _existing(session, ObjectiveRecord, operation.objective_id)
        if task_id == operation.task_id and objective_id == operation.objective_id:
            return operation
        return operation.model_copy(update={"task_id": task_id, "objective_id": objective_id})

    @staticmethod
    async def _requirement(ctx: AgentContext, operation: Operation) -> RequirementFacts | None:
        requirement_id = operation.declared_requirement_id
        if requirement_id is None:
            return None
        async with ctx.sessions() as session:
            row = await session.get(RequirementRecord, requirement_id)
        if row is None:
            return None
        criteria = " ".join(str(item) for item in row.acceptance_criteria or [])
        return RequirementFacts(
            requirement_id=row.id,
            status=RequirementStatus(row.status),
            text=f"{row.title} {row.description} {criteria}",
        )

    @staticmethod
    def _scope(
        assessment: Assessment, review_message_id: UUID | None, block_reason: str | None
    ) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "needs_user_authorization": assessment.needs_user_authorization,
            "checks": [check.name for check in assessment.checks],
        }
        if review_message_id is not None:
            scope["review_message_id"] = str(review_message_id)
        if block_reason is not None:
            scope["block_reason"] = block_reason
        if assessment.provenance is not None:
            scope["provenance"] = {
                "source": assessment.provenance.source,
                "integrity": assessment.provenance.integrity,
                "version": assessment.provenance.version,
            }
        return scope

    @staticmethod
    def _evidence(
        operation: Operation,
        assessment: Assessment,
        evidence: Sequence[Mapping[str, Any]],
        summary: RiskSummary | None,
    ) -> list[Mapping[str, Any] | UntrustedContent]:
        entries: list[Mapping[str, Any] | UntrustedContent] = [
            check.as_evidence() for check in assessment.checks
        ]
        for index, item in enumerate(list(evidence)[:_EVIDENCE_ITEMS]):
            # Supplied evidence is kept as quoted, hashed data, never as structured claims.
            entries.append(
                UntrustedContent(
                    source=f"{operation.requested_by.value}:evidence[{index}]",
                    content=json.dumps(item, sort_keys=True, default=str),
                    retrieved_at=datetime.now(UTC),
                )
            )
        if summary is not None:
            entries.append(
                {
                    "type": "advisory_risk_summary",
                    "advisory": True,
                    "summary": summary.summary,
                }
            )
        return entries

    async def _risk_summary(
        self,
        operation: Operation,
        assessment: Assessment,
        evidence: Sequence[Mapping[str, Any]],
        ctx: AgentContext,
    ) -> RiskSummary | None:
        """Advisory LLM summary of untrusted evidence; any failure falls back to none."""
        if not self._summarize_risks or ctx.models is None or not evidence:
            return None
        request = CompletionRequest(
            messages=(
                CompletionMessage(role="system", content=RISK_SUMMARY_SYSTEM),
                CompletionMessage(
                    role="user",
                    content=risk_summary_prompt(operation, assessment.outcome.value, evidence),
                ),
            ),
            tier=ModelTier.REASONING,
            response_format="json_object",
            max_tokens=600,
            temperature=0,
        )
        try:
            response = await ctx.models.complete(
                request,
                ModelCallContext(task_id=operation.task_id, objective_id=operation.objective_id),
            )
            return RiskSummary.model_validate_json(response.text)
        except Exception as error:  # noqa: BLE001 - advisory only, never affects the decision
            ctx.logger.warning(
                "WARDEN risk summary unavailable: %s",
                type(error).__name__,
                extra={"input_hash": operation.input_hash},
            )
            return None

    async def _send(
        self,
        target: AgentId,
        payload: Payload,
        ctx: AgentContext,
        *,
        key: str,
        operation: Operation,
        correlation_id: UUID | None,
    ) -> None:
        envelope: dict[str, Any] = {}
        async with ctx.sessions() as session:
            if await _existing(session, ObjectiveRecord, operation.objective_id) is not None:
                envelope["objective_id"] = operation.objective_id
            if (
                operation.task_id is not None
                and await task_ref_in(session, operation.task_id) is not None
            ):
                envelope["task_id"] = operation.task_id
            if (
                await _existing(session, RequirementRecord, operation.declared_requirement_id)
                is not None
            ):
                envelope["requirement_id"] = operation.declared_requirement_id
        if correlation_id is not None:
            envelope["correlation_id"] = correlation_id
        message = build_message(
            self.agent_id,
            target,
            payload,
            message_id=_derived_id(key, target.value),
            **envelope,
        )
        await ctx.bus.publish(message)


__all__ = ["WARDEN_NAMESPACE", "SweepResult", "WardenAgent"]
