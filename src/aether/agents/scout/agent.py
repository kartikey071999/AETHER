"""SCOUT queues scheduler-owned work, investigates, and consolidates persisted findings."""

import json
from uuid import UUID, uuid5

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from aether.agents.base import (
    AgentContext,
    BaseAgent,
    TaskResult,
    TaskResultStatus,
    UnsupportedMessageType,
)
from aether.agents.scout import prompts, workflow
from aether.agents.scout.schemas import (
    FileSourceResult,
    FindingDraft,
    GeneratedObjective,
    HttpSourceResult,
    ResearchLimits,
    ResearchWork,
)
from aether.communication.contracts import (
    BlockerPayload,
    ResearchBudget,
    ResearchFindingPayload,
    ResearchMode,
    ResearchRequestPayload,
    SecurityDecisionPayload,
    TaskAssignPayload,
    TestReportPayload,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.models.base import ProviderError
from aether.models.contracts import CompletionMessage, CompletionRequest, ModelTier
from aether.models.facade import ModelCallContext, ModelUnavailable
from aether.security import DecisionOutcome
from aether.security.untrusted import UntrustedContent
from aether.storage.models import (
    MessageRecord,
    ObjectiveRecord,
    SecurityDecisionRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import TaskRepository
from aether.storage.repositories.security import SecurityDecisionRepository


class ScoutAgent(BaseAgent):
    agent_id = AgentId.SCOUT
    accepted_message_types = receivable_types(AgentId.SCOUT) & frozenset(
        {
            MessageType.RESEARCH_REQUEST,
            MessageType.RESEARCH_FINDING,
            MessageType.TASK_ASSIGN,
            MessageType.BLOCKER,
            MessageType.TEST_REPORT,
            MessageType.SECURITY_DECISION,
        }
    )

    def __init__(self, limits: ResearchLimits | None = None) -> None:
        super().__init__()
        self.limits = limits or ResearchLimits()

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        self.ensure_accepts(message)
        if message.target_agent is not self.agent_id:
            raise ValueError("message is addressed to a different agent")
        payload = validate_message(message)
        if isinstance(payload, ResearchRequestPayload):
            if self.agent_id is AgentId.ROGUE and payload.mode is not ResearchMode.UNCONVENTIONAL:
                raise ValueError("ROGUE requires mode=unconventional")
            await workflow.enqueue(self.agent_id, message, payload, self.limits, ctx)
        elif isinstance(payload, ResearchFindingPayload) and self.agent_id is AgentId.SCOUT:
            await workflow.consolidate(message, payload, self.limits, ctx)
        elif isinstance(payload, TaskAssignPayload):
            async with ctx.sessions() as session:
                task = await session.get(TaskRecord, payload.task_id)
            if task is None or task.assigned_agent != self.agent_id.value:
                raise LookupError("assigned research task must be persisted for this agent")
            if task.objective_id != payload.objective_id:
                raise ValueError("assigned research task does not match the objective")
            # The scheduler/worker owns execution; receiving assignment never runs it inline.
        elif isinstance(payload, BlockerPayload):
            if payload.route_to is not self.agent_id:
                raise ValueError("blocker is routed to a different agent")
            objective_id = payload.objective_id
            if objective_id is None:
                async with ctx.sessions() as session:
                    task = await session.get(TaskRecord, payload.task_id)
                    if task is None:
                        raise LookupError("blocked task does not exist")
                    objective_id = task.objective_id
            await workflow.enqueue(
                self.agent_id,
                message,
                ResearchRequestPayload(
                    objective_id=objective_id,
                    task_id=payload.task_id,
                    question=f"Find alternatives after the standard approach failed: {payload.description}",
                    constraints=[
                        "Do not repeat failed approaches",
                        "No implementation or scanning",
                    ],
                    mode=(
                        ResearchMode.UNCONVENTIONAL
                        if self.agent_id is AgentId.ROGUE
                        else ResearchMode.CONVENTIONAL
                    ),
                    budget=self._budget(),
                ),
                self.limits,
                ctx,
            )
        elif isinstance(payload, TestReportPayload):
            if payload.verdict.value != "failed":
                ctx.logger.info(
                    "research.verification_notification", extra={"agent": self.agent_id.value}
                )
                return
            if payload.task_id is None:
                await workflow.generate_objective(
                    self.agent_id,
                    GeneratedObjective(
                        question=f"Investigate failed verification in {payload.suite}",
                        rationale="Failed verification indicates a research gap",
                        expected_benefit="Find a feasible alternative to the failed approach",
                        estimated_cost=0,
                        stopping_conditions=["Stop after comparing bounded alternatives"],
                    ),
                    self.limits,
                    ctx,
                    origin_message_id=message.message_id,
                    correlation_id=message.correlation_id,
                )
                return
            async with ctx.sessions() as session:
                task = await session.get(TaskRecord, payload.task_id)
                if task is None:
                    raise LookupError("verification task does not exist")
            await workflow.enqueue(
                self.agent_id,
                message,
                ResearchRequestPayload(
                    objective_id=task.objective_id,
                    task_id=task.id,
                    question=f"Research alternatives for failed verification: {task.title}",
                    mode=(
                        ResearchMode.UNCONVENTIONAL
                        if self.agent_id is AgentId.ROGUE
                        else ResearchMode.CONVENTIONAL
                    ),
                    constraints=["Use failure-driven alternatives"],
                    budget=self._budget(),
                ),
                self.limits,
                ctx,
            )
        elif isinstance(payload, SecurityDecisionPayload):
            await self._security_decision(payload, message, ctx)
        else:
            raise UnsupportedMessageType(
                self.agent_id, message.message_type, self.accepted_message_types
            )

    def _budget(self) -> ResearchBudget:
        return ResearchBudget(
            max_sources=self.limits.max_sources, max_llm_requests=self.limits.max_llm_requests
        )

    async def generate_objective(self, proposal: GeneratedObjective, ctx: AgentContext) -> UUID:
        return await workflow.generate_objective(self.agent_id, proposal, self.limits, ctx)

    async def _security_decision(
        self, payload: SecurityDecisionPayload, message: AgentMessage, ctx: AgentContext
    ) -> None:
        async with workflow.atomic_research(ctx) as transaction:
            async with transaction.sessions() as session:
                decision = await session.get(SecurityDecisionRecord, payload.decision_id)
                if (
                    decision is None
                    or decision.input_hash != payload.input_hash
                    or decision.decision != payload.decision.value
                    or decision.task_id is None
                    or decision.scope.get("requested_by") != self.agent_id.value
                ):
                    raise ValueError("security decision does not match persisted WARDEN review")
                if decision.expires_at and workflow.as_utc(decision.expires_at) <= ctx.clock():
                    raise ValueError("security decision has expired")
                task = await session.get(TaskRecord, decision.task_id)
                latest = await SecurityDecisionRepository.latest_for_input_hash(
                    session, payload.input_hash
                )
                review = await session.scalar(
                    select(MessageRecord).where(
                        MessageRecord.source_agent == self.agent_id.value,
                        MessageRecord.task_id == decision.task_id,
                        MessageRecord.message_type == MessageType.SECURITY_EVALUATE.value,
                        MessageRecord.payload["input_hash"].as_string() == payload.input_hash,
                    )
                )
                if latest is None or latest.id != decision.id or review is None:
                    raise ValueError("decision is stale or does not match a requested review")
            if task is None or task.assigned_agent != self.agent_id.value:
                raise LookupError("security decision references an unknown research task")
            if task.state != TaskStatus.WAITING_FOR_APPROVAL:
                ctx.logger.info(
                    "research.security_decision_no_waiter", extra={"task_id": str(task.id)}
                )
                return
            await TaskRepository(transaction.sessions).transition(
                task.id,
                TaskStatus.READY
                if payload.decision is DecisionOutcome.APPROVED
                else TaskStatus.BLOCKED,
                actor=self.agent_id.value,
                details={"reason": "WARDEN research decision"},
                correlation_id=message.correlation_id,
            )

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        if task.assigned_agent != self.agent_id.value:
            return TaskResult(
                status=TaskResultStatus.FAILED,
                error="task belongs to a different agent",
                retryable=False,
            )
        existing = await workflow.stored_finding(self.agent_id, task.id, ctx)
        if existing is not None:
            return self._finding_result(existing.id)
        try:
            if task.description.lstrip().startswith("{"):
                work = ResearchWork.model_validate_json(task.description)
                if work.request.objective_id != task.objective_id:
                    raise ValueError("research work does not match task objective")
                work = work.model_copy(
                    update={"request": work.request.model_copy(update={"task_id": task.id})}
                )
            else:
                async with ctx.sessions() as session:
                    assignment = await session.scalar(
                        select(MessageRecord)
                        .where(
                            MessageRecord.task_id == task.id,
                            MessageRecord.target_agent == self.agent_id.value,
                            MessageRecord.message_type == MessageType.TASK_ASSIGN.value,
                        )
                        .order_by(MessageRecord.timestamp.desc())
                        .limit(1)
                    )
                work = ResearchWork(
                    request=ResearchRequestPayload(
                        objective_id=task.objective_id,
                        task_id=task.id,
                        question=task.description.strip() or task.title,
                        budget=self._budget(),
                        review_by=(workflow.as_utc(task.deadline) if task.deadline else None),
                        mode=(
                            ResearchMode.UNCONVENTIONAL
                            if self.agent_id is AgentId.ROGUE
                            else ResearchMode.CONVENTIONAL
                        ),
                    ),
                    correlation_id=(
                        assignment.correlation_id
                        if assignment
                        else uuid5(task.id, "research-correlation")
                    ),
                    origin_message_id=task.id,
                )
            request = workflow.bounded_request(work.request, self.limits, ctx.clock())
            work = work.model_copy(update={"request": request})
            sources = workflow.investigations(request, self.limits)
        except (ValidationError, ValueError) as error:
            ctx.logger.warning("research.invalid_work", extra={"error_type": type(error).__name__})
            return TaskResult(
                status=TaskResultStatus.BLOCKED,
                error="invalid or expired research work",
                retryable=False,
            )
        async with ctx.sessions() as session:
            objective = await session.get(ObjectiveRecord, task.objective_id)
        if (
            objective is None
            or objective.status != "active"
            or (objective.review_at and workflow.as_utc(objective.review_at) <= ctx.clock())
        ):
            return TaskResult(
                status=TaskResultStatus.BLOCKED,
                error="objective is inactive or due for review",
                retryable=False,
            )
        contents: list[UntrustedContent] = []
        for source in sources:
            if self.is_cancelled(task.id) or ctx.clock() >= request.review_by:
                return TaskResult(
                    status=TaskResultStatus.BLOCKED,
                    error="research stopped at cancellation/review",
                    retryable=False,
                )
            if not await workflow.reserve_budget(
                self.agent_id,
                work,
                "source",
                request.budget.max_sources,
                ctx,
                reservation_key=uuid5(task.id, f"source:{source.kind}:{source.location}"),
            ):
                break
            invocation = await self.invoke_tool(
                "http_request" if source.kind == "http" else "read_file",
                {"url": source.location, "method": "GET"}
                if source.kind == "http"
                else {"path": source.location, "max_bytes": 16384},
                ctx,
                task=task,
                correlation_id=work.correlation_id,
            )
            if invocation.result is not None:
                return invocation.result
            if isinstance(invocation.value, HttpSourceResult):
                if not 200 <= invocation.value.status_code < 300:
                    return TaskResult(
                        status=TaskResultStatus.FAILED,
                        error=f"research source returned HTTP {invocation.value.status_code}",
                    )
                content = invocation.value.body
            elif isinstance(invocation.value, FileSourceResult):
                content = invocation.value.content
            elif isinstance(invocation.value, UntrustedContent):
                content = invocation.value
            else:
                return TaskResult(
                    status=TaskResultStatus.FAILED,
                    error="tool did not return untrusted source data",
                    retryable=False,
                )
            if not isinstance(content, UntrustedContent):
                return TaskResult(
                    status=TaskResultStatus.FAILED,
                    error="source content is not marked untrusted",
                    retryable=False,
                )
            contents.append(content)
        draft = await self.summarize(work, contents, ctx)
        if self.is_cancelled(task.id) or ctx.clock() >= request.review_by:
            return TaskResult(
                status=TaskResultStatus.BLOCKED,
                error="research stopped before persistence",
                retryable=False,
            )
        finding_id = await workflow.save_finding(self.agent_id, work, draft, contents, ctx)
        return self._finding_result(finding_id)

    @staticmethod
    def _finding_result(finding_id: UUID) -> TaskResult:
        return TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary="Research finding persisted for SCOUT consolidation",
            outputs={"finding_id": str(finding_id)},
        )

    async def complete_json[T: BaseModel](
        self,
        schema: type[T],
        instructions: str,
        data: str,
        work: ResearchWork,
        ctx: AgentContext,
        *,
        kind: str = "llm",
        limit: int | None = None,
    ) -> T | None:
        if ctx.models is None:
            ctx.logger.info("research.model_fallback", extra={"reason": "not_configured"})
            return None
        if not await workflow.reserve_budget(
            self.agent_id,
            work,
            kind,
            work.request.budget.max_llm_requests if limit is None else limit,
            ctx,
        ):
            return None
        try:
            response = await ctx.models.complete(
                CompletionRequest(
                    tier=ModelTier.LIGHT,
                    response_format="json_schema",
                    json_schema=schema.model_json_schema(),
                    max_tokens=1024,
                    max_tokens_cap=1024,
                    messages=(
                        CompletionMessage(role="system", content=instructions),
                        CompletionMessage(role="user", content=data),
                    ),
                ),
                ModelCallContext(
                    task_id=work.request.task_id, objective_id=work.request.objective_id
                ),
            )
            return schema.model_validate_json(response.text)
        except (ModelUnavailable, ProviderError, ValidationError, TimeoutError) as error:
            ctx.logger.warning("research.model_fallback", extra={"reason": type(error).__name__})
            return None

    @staticmethod
    def prompt_data(work: ResearchWork, contents: list[UntrustedContent]) -> str:
        question = UntrustedContent(
            source="research-question",
            content=json.dumps(
                {
                    "question": work.request.question,
                    "constraints": work.request.constraints,
                    "scope": work.request.scope,
                }
            ),
        ).quote(4096)
        return (
            question
            + "\n"
            + "\n".join(
                f"Evidence index {index}:\n{content.quote(4096)}"
                for index, content in enumerate(contents)
            )
        )

    async def summarize(
        self, work: ResearchWork, contents: list[UntrustedContent], ctx: AgentContext
    ) -> FindingDraft:
        if any(content.suspicious_patterns() for content in contents):
            ctx.logger.warning("research.source_quarantined", extra={"agent": self.agent_id.value})
            return workflow.fallback(work.request.question, contents)
        draft = await self.complete_json(
            FindingDraft, prompts.SUMMARIZE, self.prompt_data(work, contents), work, ctx
        )
        if draft is None:
            return workflow.fallback(work.request.question, contents)
        if any(index < 0 or index >= len(contents) for index in draft.evidence_indices):
            ctx.logger.warning("research.invalid_citation", extra={"agent": self.agent_id.value})
            return workflow.fallback(work.request.question, contents)
        candidates = sorted(draft.candidates, key=lambda item: item.name != draft.recommendation)
        return draft.model_copy(update={"candidates": candidates[: self.limits.top_k]})
