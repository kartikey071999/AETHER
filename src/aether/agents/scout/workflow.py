"""Persistent, bounded research services shared with ROGUE; no privileged effects."""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aether.agents.base import AgentContext
from aether.agents.scout.schemas import (
    FindingDraft,
    GeneratedObjective,
    ResearchLimits,
    ResearchScope,
    ResearchWork,
    SourceSpec,
)
from aether.agents.work_queue import MAX_LIMIT, work_queue
from aether.communication.bus import MessageBus
from aether.communication.contracts import (
    CandidateApproach,
    ConsolidationDecision,
    EvaluationOutcome,
    EvidenceItem,
    EvidenceTrust,
    FindingDecision,
    Payload,
    ProgressUpdatePayload,
    RequirementEvaluatedPayload,
    RequirementSubmitPayload,
    RequirementType,
    ResearchBudget,
    ResearchConsolidatedPayload,
    ResearchFindingPayload,
    ResearchMode,
    ResearchRequestPayload,
    ResourceEstimate,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.untrusted import UntrustedContent
from aether.storage.models import (
    CapabilityStatus,
    FindingClassification,
    FindingStatus,
    MessageRecord,
    ObjectiveRecord,
    RequirementRecord,
    RequirementStatus,
    ResearchFindingRecord,
    TaskRecord,
    TaskStatus,
)
from aether.storage.repositories import (
    CapabilityRepository,
    ObjectiveRepository,
    RequirementRepository,
    ResearchFindingRepository,
    TaskRepository,
)
from aether.storage.repositories.research import EvidenceInput
from aether.storage.repositories.tasks import ALLOWED_TASK_TRANSITIONS


class ResearchLimitReached(ValueError):
    """Admission was refused rather than silently growing the queue."""


_OPEN_REQUIREMENTS = (
    RequirementStatus.PROPOSED.value,
    RequirementStatus.APPROVED.value,
    RequirementStatus.IN_PROGRESS.value,
    RequirementStatus.SATISFIED.value,
)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@asynccontextmanager
async def atomic_research(ctx: AgentContext) -> AsyncIterator[AgentContext]:
    """Compose unchanged repositories and bus writes into one short transaction.

    Savepoint-bound sessions let each repository retain its normal transaction API.
    Serialize admission/consolidation, including processes, without holding locks
    during tool or model calls.
    """
    async with ctx.sessions() as outer, outer.begin():
        connection = await outer.connection()
        if connection.dialect.name == "sqlite":
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
        elif connection.dialect.name == "postgresql":
            await outer.execute(text("SELECT pg_advisory_xact_lock(310031)"))
        else:
            raise ValueError("research transactions require SQLite or PostgreSQL")
        sessions = async_sessionmaker(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        yield replace(ctx, sessions=sessions, bus=MessageBus(sessions, clock=ctx.clock))
    for agent in AgentId:
        ctx.bus.wake(agent)


async def publish(
    agent: AgentId,
    target: AgentId,
    payload: Payload,
    ctx: AgentContext,
    *,
    key: UUID,
    suffix: str,
    correlation_id: UUID,
) -> None:
    # The shared sender table currently over-permits ROGUE; enforce its role locally.
    if agent is AgentId.ROGUE and not (
        isinstance(payload, ResearchFindingPayload)
        and target is AgentId.SCOUT
        or isinstance(payload, ProgressUpdatePayload)
        and target is AgentId.LEDGER
    ):
        raise PermissionError("ROGUE may only submit findings to SCOUT and audited progress")
    await ctx.bus.publish(
        build_message(
            agent,
            target,
            payload,
            message_id=uuid5(key, suffix),
            correlation_id=correlation_id,
            objective_id=getattr(payload, "objective_id", None),
            task_id=getattr(payload, "task_id", None),
            requirement_id=getattr(payload, "requirement_id", None),
            timestamp=ctx.clock(),
        )
    )


def bounded_request(
    request: ResearchRequestPayload, limits: ResearchLimits, now: datetime
) -> ResearchRequestPayload:
    deadline = min(request.review_by or now + limits.review_interval, now + limits.review_interval)
    if deadline <= now:
        raise ResearchLimitReached("research review date has elapsed")
    return request.model_copy(
        update={
            "budget": ResearchBudget(
                max_sources=min(request.budget.max_sources, limits.max_sources),
                max_llm_requests=min(request.budget.max_llm_requests, limits.max_llm_requests),
            ),
            "review_by": deadline,
        }
    )


async def enqueue(
    agent: AgentId,
    message: AgentMessage,
    request: ResearchRequestPayload,
    limits: ResearchLimits,
    ctx: AgentContext,
) -> UUID:
    async with atomic_research(ctx) as transaction:
        return await _enqueue(agent, message, request, limits, transaction)


async def _enqueue(
    agent: AgentId,
    message: AgentMessage,
    request: ResearchRequestPayload,
    limits: ResearchLimits,
    ctx: AgentContext,
) -> UUID:
    key = f"research:{agent.value}:{message.message_id}"
    queue = work_queue(ctx, agent)
    existing = await queue.task_by_key(key)
    if existing is not None:
        return existing.id
    # FAILED tasks may still be retried, so they keep occupying the research queue.
    tasks = [
        *await queue.open_tasks(limit=MAX_LIMIT),
        *await queue.finished_tasks(states={TaskStatus.FAILED}, limit=MAX_LIMIT),
    ]
    async with ctx.sessions() as session:
        objective = await session.get(ObjectiveRecord, request.objective_id)
        if objective is None:
            raise LookupError("research objective does not exist")
        if objective.status != "active":
            raise ResearchLimitReached("research objective is not active")
        request = bounded_request(request, limits, ctx.clock())
        if objective.review_at and as_utc(objective.review_at) <= ctx.clock():
            raise ResearchLimitReached("objective review date has elapsed")
        if request.task_id is not None:
            parent = await session.get(TaskRecord, request.task_id)
            if parent is None or parent.objective_id != request.objective_id:
                raise ValueError("research parent task must belong to the owning objective")
        investigations(request, limits)
        expired_ids: set[UUID] = set()
        for task in tasks:
            if (
                task.deadline
                and task.deadline <= ctx.clock()
                and TaskStatus.CANCELLED in ALLOWED_TASK_TRANSITIONS[task.state]
            ):
                expired_ids.add(task.id)
        generated = list(
            await session.scalars(
                select(ObjectiveRecord).where(
                    ObjectiveRecord.created_by == agent.value,
                    ObjectiveRecord.generated_by_agent.is_(True),
                    ObjectiveRecord.status == "active",
                )
            )
        )
        generated_ids: set[UUID] = set()
        expired_objective_ids: set[UUID] = set()
        for generated_objective in generated:
            if (
                generated_objective.review_at
                and as_utc(generated_objective.review_at) <= ctx.clock()
            ):
                expired_objective_ids.add(generated_objective.id)
            else:
                generated_ids.add(generated_objective.id)
        # Completed findings can precede the runtime's final task transition.
        completed_ids = set(
            await session.scalars(
                select(ResearchFindingRecord.task_id).where(
                    ResearchFindingRecord.agent == agent.value
                )
            )
        )
        tasks = [task for task in tasks if task.id not in completed_ids | expired_ids]
        if len(tasks) >= limits.max_queue_size:
            raise ResearchLimitReached("research task queue is full")
        objectives = {task.objective_id for task in tasks} | generated_ids | {request.objective_id}
        if len(objectives) > limits.max_active_objectives:
            raise ResearchLimitReached("active research objective limit reached")
    for task_id in expired_ids:
        await TaskRepository(ctx.sessions).transition(
            task_id,
            TaskStatus.CANCELLED,
            actor=agent.value,
            details={"reason": "research review date elapsed"},
            correlation_id=message.correlation_id,
        )
    for objective_id in expired_objective_ids:
        await ObjectiveRepository(ctx.sessions).set_status(
            objective_id,
            "paused",
            actor=agent.value,
            reason="generated research objective is due for review",
            correlation_id=message.correlation_id,
        )
    work = ResearchWork(
        request=request, correlation_id=message.correlation_id, origin_message_id=message.message_id
    )
    repository = TaskRepository(ctx.sessions)
    task_id = await repository.create(
        objective_id=request.objective_id,
        title=f"{agent.value}: {request.question}"[:200],
        description=work.model_dump_json(),
        assigned_agent=agent.value,
        parent_task_id=request.task_id,
        deadline=request.review_by,
        max_attempts=limits.max_attempts,
        idempotency_key=key,
        actor=agent.value,
        correlation_id=message.correlation_id,
    )
    await repository.transition(
        task_id,
        TaskStatus.READY,
        actor=agent.value,
        details={"reason": "bounded research admitted for scheduling"},
        correlation_id=message.correlation_id,
    )
    return task_id


async def generate_objective(
    agent: AgentId,
    proposal: GeneratedObjective,
    limits: ResearchLimits,
    ctx: AgentContext,
    *,
    origin_message_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> UUID:
    async with atomic_research(ctx) as transaction:
        now = ctx.clock()
        if origin_message_id is not None:
            existing = await work_queue(transaction, agent).task_by_key(
                f"research:{agent.value}:{origin_message_id}"
            )
            if existing is not None:
                return existing.objective_id
        async with transaction.sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ObjectiveRecord)
                .where(
                    ObjectiveRecord.created_by == agent.value,
                    ObjectiveRecord.generated_by_agent.is_(True),
                    ObjectiveRecord.created_at
                    >= now - timedelta(seconds=limits.generation_interval_seconds),
                )
            )
            if (count or 0) >= limits.max_generated_per_interval:
                raise ResearchLimitReached("generated research objective interval limit reached")
        objective_id = await ObjectiveRepository(transaction.sessions).create(
            title=proposal.question[:200],
            description=proposal.question,
            created_by=agent.value,
            rationale=proposal.rationale,
            expected_benefit=proposal.expected_benefit,
            estimated_cost=proposal.estimated_cost,
            stopping_conditions=[
                *proposal.stopping_conditions,
                "Stop when source/model budget is exhausted",
                "Stop at the review date",
            ],
            completion_criteria=["Persist an evidence-linked comparison and recommendation"],
            review_at=now + limits.review_interval,
            generated_by_agent=True,
        )
        request = ResearchRequestPayload(
            objective_id=objective_id,
            question=proposal.question,
            scope=proposal.scope,
            mode=ResearchMode.UNCONVENTIONAL
            if agent is AgentId.ROGUE
            else ResearchMode.CONVENTIONAL,
            budget=ResearchBudget(
                max_sources=limits.max_sources, max_llm_requests=limits.max_llm_requests
            ),
            review_by=now + limits.review_interval,
        )
        # ROGUE cannot send research.request; this envelope is internal persisted work only.
        message = AgentMessage(
            source_agent=AgentId.ORCHESTR,
            target_agent=agent,
            message_type=MessageType.RESEARCH_REQUEST,
            payload=request.model_dump(mode="json"),
            message_id=origin_message_id or uuid5(objective_id, "self-generated"),
            correlation_id=correlation_id or uuid5(objective_id, "research-correlation"),
        )
        await _enqueue(agent, message, request, limits, transaction)
        return objective_id


def investigations(request: ResearchRequestPayload, limits: ResearchLimits) -> list[SourceSpec]:
    scope = request.scope.strip()
    sources = (
        ResearchScope.model_validate_json(scope).sources
        if scope.startswith("{")
        else list(limits.default_sources)
    )
    unique = {
        (source.kind, source.location): source
        for source in sources
        if source.perspective in {"both", request.mode.value}
    }
    selected = list(unique.values())
    if request.mode is ResearchMode.UNCONVENTIONAL:
        selected.reverse()
    return selected[: request.budget.max_sources]


async def reserve_budget(
    agent: AgentId,
    work: ResearchWork,
    kind: str,
    limit: int,
    ctx: AgentContext,
    *,
    reservation_key: UUID | None = None,
) -> bool:
    async with atomic_research(ctx) as transaction:
        async with transaction.sessions() as session:
            if (
                reservation_key is not None
                and await session.get(MessageRecord, uuid5(reservation_key, "source-reservation"))
                is not None
            ):
                return True
            used = await session.scalar(
                select(func.count())
                .select_from(MessageRecord)
                .where(
                    MessageRecord.source_agent == agent.value,
                    MessageRecord.objective_id == work.request.objective_id,
                    MessageRecord.message_type == MessageType.PROGRESS_UPDATE.value,
                    MessageRecord.payload["details"]["research_budget"].as_string() == kind,
                )
            )
        used = used or 0
        if used >= limit:
            ctx.logger.info("research.budget_exhausted", extra={"agent": agent.value, "kind": kind})
            return False
        await publish(
            agent,
            AgentId.LEDGER,
            ProgressUpdatePayload(
                objective_id=work.request.objective_id,
                task_id=work.request.task_id,
                summary=f"Research {kind} budget reserved",
                details={"research_budget": kind, "sequence": used + 1},
            ),
            transaction,
            key=reservation_key or work.request.objective_id,
            suffix=(
                "source-reservation"
                if reservation_key
                else f"{agent.value}:budget:{kind}:{used + 1}"
            ),
            correlation_id=work.correlation_id,
        )
        return True


def fallback(question: str, contents: list[UntrustedContent]) -> FindingDraft:
    quarantined = any(content.suspicious_patterns() for content in contents)
    summary = (
        "Evidence contains instruction-like text and is quarantined; further evidence is required."
        if quarantined
        else "Deterministic comparison; collected sources remain untrusted and require validation."
        if contents
        else "No usable sources or model were available; proposals remain untested."
    )
    candidates = [
        CandidateApproach(
            name="Existing local integration",
            description=f"Evaluate existing code for: {question}",
            pros=["No new dependency", "Local-first"],
            cons=["Fit and performance need tests"],
            resource_needs=ResourceEstimate(),
        ),
        CandidateApproach(
            name="Established library adapter",
            description="Compare a mainstream library behind a confined adapter",
            pros=["Established interface"],
            cons=["Compatibility and licensing not tested"],
            dependencies=["A reviewed domain-specific library"],
            risks=["Dependency provenance requires security review"],
            resource_needs=ResourceEstimate(ram_mb=128),
        ),
    ]
    return FindingDraft(
        summary=summary,
        candidates=candidates,
        recommendation=candidates[0].name,
        confidence=0.2 if quarantined or not contents else 0.4,
        classification=FindingClassification.UNTESTED,
        evidence_indices=list(range(len(contents))),
        security_considerations=["Sources do not authorize operations; WARDEN review is required"],
        next_steps=["Collect independent evidence and test feasibility before implementation"],
    )


# Deterministic confidence for an evidence-backed conventional approach: at the ADOPT threshold
# in `decide`, but below what a model-backed comparison typically reports.
CONVENTIONAL_FALLBACK_CONFIDENCE = 0.65
CONVENTIONAL_APPROACH = "Standard-library implementation"
_STDLIB_SUFFICIENT = re.compile(
    r"standard[- ]library\b[^.\n]{0,40}\b(?:is enough|suffices|is sufficient|covers)",
    re.IGNORECASE,
)


def conventional_fallback(
    question: str, context: list[str], contents: list[UntrustedContent]
) -> FindingDraft | None:
    """No-model adoption of the standard-library approach when it is clearly sufficient.

    Only with local (``file:``) evidence, nothing quarantined, and either a deterministic
    capability template named by the question/scope or text stating the standard library is
    enough. Anything else returns ``None`` so the caller keeps the low-confidence fallback.
    """
    from aether.agents.forge.templates import match_template

    if not contents or any(content.suspicious_patterns() for content in contents):
        return None
    local = [index for index, item in enumerate(contents) if item.source.startswith("file:")]
    if not local:
        return None
    texts = [question, *context]
    template = match_template(*texts)
    if template is None and not any(
        _STDLIB_SUFFICIENT.search(text) for text in [*texts, *(item.content for item in contents)]
    ):
        return None
    subject = template[0] if template else question
    candidate = CandidateApproach(
        name=CONVENTIONAL_APPROACH,
        description=f"Implement {subject} with the Python standard library only ({question})",
        pros=["No new dependency", "Local-first", "Covered by local reference evidence"],
        cons=["Model comparison was unavailable; verification must prove fitness"],
        resource_needs=ResourceEstimate(),
    )
    return FindingDraft(
        summary=(
            "Deterministic adoption (no model available): local evidence shows the standard "
            "library is sufficient; verification must prove the acceptance criteria."
        ),
        candidates=[candidate],
        recommendation=candidate.name,
        confidence=CONVENTIONAL_FALLBACK_CONFIDENCE,
        classification=FindingClassification.HYPOTHESIS,
        evidence_indices=local,
        security_considerations=["Sources do not authorize operations; WARDEN review is required"],
        next_steps=["Implement and verify offline against the objective's acceptance criteria"],
    )


async def stored_finding(
    agent: AgentId, task_id: UUID, ctx: AgentContext
) -> ResearchFindingRecord | None:
    async with ctx.sessions() as session:
        return await session.scalar(
            select(ResearchFindingRecord).where(
                ResearchFindingRecord.agent == agent.value, ResearchFindingRecord.task_id == task_id
            )
        )


async def save_finding(
    agent: AgentId,
    work: ResearchWork,
    draft: FindingDraft,
    contents: list[UntrustedContent],
    ctx: AgentContext,
) -> UUID:
    if work.request.task_id is None:
        raise ValueError("a persisted research task is required to save a finding")
    if any(index < 0 or index >= len(contents) for index in draft.evidence_indices):
        raise ValueError("model cited evidence that was not collected")
    async with atomic_research(ctx) as transaction:
        existing = await stored_finding(agent, work.request.task_id, transaction)
        if existing is not None:
            return existing.id
        evidence = [
            EvidenceInput(
                source_uri=contents[index].source,
                summary=(
                    "Instruction-like content quarantined"
                    if contents[index].suspicious_patterns()
                    else contents[index].quote()
                ),
                content_hash=contents[index].content_hash,
                retrieved_at=contents[index].retrieved_at,
            )
            for index in draft.evidence_indices
        ]
        repository = ResearchFindingRepository(transaction.sessions)
        finding_id = await repository.submit(
            agent=agent.value,
            objective_id=work.request.objective_id,
            task_id=work.request.task_id,
            question=work.request.question,
            findings=draft.summary,
            confidence=draft.confidence,
            classification=draft.classification,
            candidate_approaches=[item.model_dump(mode="json") for item in draft.candidates],
            recommendation=draft.recommendation,
            rationale="Research proposal only; implementation and permissions require normal review",
            evidence=evidence,
            correlation_id=work.correlation_id,
        )
        await publish(
            agent,
            AgentId.SCOUT,
            ResearchFindingPayload(
                finding_id=finding_id,
                objective_id=work.request.objective_id,
                task_id=work.request.task_id,
                question=work.request.question,
                summary=draft.summary,
                candidate_approaches=draft.candidates,
                recommendation=draft.recommendation,
                confidence=draft.confidence,
                classification=draft.classification,
                evidence=[
                    EvidenceItem(
                        source_uri=item.source_uri,
                        summary=item.summary,
                        content_hash=item.content_hash,
                        trust=EvidenceTrust.UNTRUSTED,
                    )
                    for item in evidence
                ],
                security_considerations=draft.security_considerations,
                next_steps=draft.next_steps,
                submitted_by=agent,
            ),
            transaction,
            key=finding_id,
            suffix="finding:SCOUT",
            correlation_id=work.correlation_id,
        )
        return finding_id


def decide(finding: ResearchFindingRecord, evidence: list[EvidenceItem]) -> FindingDecision:
    candidates = [CandidateApproach.model_validate(item) for item in finding.candidate_approaches]
    if not candidates or finding.recommendation not in {item.name for item in candidates}:
        decision, rationale = ConsolidationDecision.REJECT, "No coherent recommended candidate"
    elif not evidence or any(
        not item.content_hash or not item.summary.strip() or "quarantined" in item.summary.lower()
        for item in evidence
    ):
        decision, rationale = (
            ConsolidationDecision.NEEDS_EVIDENCE,
            "Usable cited evidence is missing",
        )
    elif finding.confidence < 0.3:
        decision, rationale = (
            ConsolidationDecision.REJECT,
            "Confidence is too low to justify adoption",
        )
    elif finding.confidence < 0.6:
        decision = ConsolidationDecision.DELEGATE_ROGUE
        rationale = "Specialized feasibility investigation is needed before adoption"
    else:
        decision = ConsolidationDecision.ADOPT
        rationale = "Evidence-linked proposal merits evaluation, not installation or verification"
    return FindingDecision(finding_id=finding.id, decision=decision, rationale=rationale)


async def consolidate(
    message: AgentMessage,
    payload: ResearchFindingPayload,
    limits: ResearchLimits,
    ctx: AgentContext,
) -> FindingDecision:
    async with atomic_research(ctx) as transaction:
        async with transaction.sessions() as session:
            finding = await session.get(ResearchFindingRecord, payload.finding_id)
            if finding is None:
                raise LookupError("submitted finding must already be persisted with its evidence")
            if (
                finding.agent != message.source_agent.value
                or finding.objective_id != payload.objective_id
                or finding.task_id != payload.task_id
            ):
                raise ValueError("finding identity does not match its persisted record")
            previous = await session.get(MessageRecord, uuid5(finding.id, "consolidated:ORCHESTR"))
            if previous is not None:
                result = ResearchConsolidatedPayload.model_validate(previous.payload)
                return result.decisions[0]
            if finding.status != FindingStatus.SUBMITTED:
                raise ValueError("finding is not available for a new consolidation")
        repository = ResearchFindingRepository(transaction.sessions)
        rows = await repository.list_evidence(finding.id)
        evidence = [
            EvidenceItem(
                source_uri=row.source_uri,
                summary=row.summary,
                content_hash=row.content_hash,
                trust=EvidenceTrust(row.trust_level),
            )
            for row in rows
        ]
        decision = decide(finding, evidence)
        async with transaction.sessions() as session:
            objective = (
                await session.get(ObjectiveRecord, finding.objective_id)
                if finding.objective_id
                else None
            )
        objective_active = (
            objective is not None
            and objective.status == "active"
            and (objective.review_at is None or as_utc(objective.review_at) > ctx.clock())
        )
        if finding.objective_id is not None and not objective_active:
            decision = FindingDecision(
                finding_id=finding.id,
                decision=ConsolidationDecision.REJECT,
                rationale="Owning objective is inactive or its review date has elapsed",
            )
        if finding.objective_id is None and decision.decision is ConsolidationDecision.ADOPT:
            decision = FindingDecision(
                finding_id=finding.id,
                decision=ConsolidationDecision.NEEDS_EVIDENCE,
                rationale="An owning objective is required before creating requirements",
            )
        if decision.decision is ConsolidationDecision.ADOPT:
            async with transaction.sessions() as session:
                adopted = await session.scalar(
                    select(RequirementRecord.id)
                    .where(
                        RequirementRecord.objective_id == finding.objective_id,
                        RequirementRecord.created_by == AgentId.SCOUT.value,
                        RequirementRecord.requirement_type == RequirementType.CAPABILITY.value,
                        RequirementRecord.status.in_(_OPEN_REQUIREMENTS),
                    )
                    .limit(1)
                )
            if adopted is not None:
                # One adopted approach per objective; later rounds must not fork the work.
                decision = FindingDecision(
                    finding_id=finding.id,
                    decision=ConsolidationDecision.REJECT,
                    rationale=(
                        "Another approach is already adopted for this objective "
                        f"(requirement {adopted}); this alternative is recorded, not adopted"
                    ),
                )
        requirement_ids: list[UUID] = []
        requirement_title = ""
        criteria: list[str] = []
        if decision.decision is ConsolidationDecision.ADOPT:
            candidate = next(
                CandidateApproach.model_validate(item)
                for item in finding.candidate_approaches
                if item["name"] == finding.recommendation
            )
            # The objective's own completion criteria are what verification must prove.
            criteria = [
                item.strip()
                for item in (objective.completion_criteria if objective else [])
                if isinstance(item, str) and item.strip()
            ] or [
                f"Demonstrate that {candidate.name} addresses: {finding.question}",
                "Provide offline automated tests for nominal and failure paths",
                "Measure resource use against the reviewed resource estimates",
                "All privileged effects use approved tools and WARDEN authorization",
            ]
            requirement_title = f"Evaluate {candidate.name}"[:200]
            requirement_id = await RequirementRepository(transaction.sessions).create(
                objective_id=finding.objective_id,
                task_id=finding.task_id,
                title=requirement_title,
                description=f"{finding.findings}\n\n{json.dumps(candidate.model_dump(mode='json'))}",
                requirement_type=RequirementType.CAPABILITY.value,
                created_by=AgentId.SCOUT.value,
                acceptance_criteria=criteria,
                source_finding_id=finding.id,
                security_review_required=True,
                correlation_id=message.correlation_id,
            )
            requirement_ids.append(requirement_id)
            await publish(
                AgentId.SCOUT,
                AgentId.LEDGER,
                RequirementSubmitPayload(
                    requirement_id=requirement_id,
                    objective_id=finding.objective_id,
                    task_id=finding.task_id,
                    title=requirement_title,
                    description=candidate.description,
                    requirement_type=RequirementType.CAPABILITY,
                    acceptance_criteria=criteria,
                    source_finding_id=finding.id,
                    security_review_required=True,
                    rationale=decision.rationale,
                ),
                transaction,
                key=finding.id,
                suffix="requirement:LEDGER",
                correlation_id=message.correlation_id,
            )
        elif (
            decision.decision
            in {ConsolidationDecision.NEEDS_EVIDENCE, ConsolidationDecision.DELEGATE_ROGUE}
            and objective_active
        ):
            async with transaction.sessions() as session:
                follow_ups = await session.scalar(
                    select(func.count())
                    .select_from(MessageRecord)
                    .where(
                        MessageRecord.source_agent == AgentId.SCOUT.value,
                        MessageRecord.target_agent == AgentId.ROGUE.value,
                        MessageRecord.objective_id == finding.objective_id,
                        MessageRecord.message_type == MessageType.RESEARCH_REQUEST.value,
                    )
                )
            if (follow_ups or 0) < limits.max_follow_ups:
                await publish(
                    AgentId.SCOUT,
                    AgentId.ROGUE,
                    ResearchRequestPayload(
                        objective_id=finding.objective_id,
                        task_id=finding.task_id,
                        question=finding.question,
                        mode=ResearchMode.UNCONVENTIONAL,
                        constraints=[
                            decision.rationale,
                            "Supply independent evidence; do not execute",
                        ],
                        budget=ResearchBudget(
                            max_sources=limits.max_sources, max_llm_requests=limits.max_llm_requests
                        ),
                        review_by=ctx.clock() + limits.review_interval,
                    ),
                    transaction,
                    key=finding.id,
                    suffix="follow-up:ROGUE",
                    correlation_id=message.correlation_id,
                )
        capabilities = CapabilityRepository(transaction.sessions)
        for item in finding.candidate_approaches[: limits.top_k]:
            candidate = CandidateApproach.model_validate(item)
            adopted = bool(requirement_ids) and candidate.name == finding.recommendation
            # Finding-scoped names preserve evidence/requirement ownership across research rounds.
            name = f"{candidate.name[:150]} [{finding.id}]"
            if await capabilities.get_by_name(name) is None:
                capability_id = await capabilities.register(
                    name=name,
                    actor=AgentId.SCOUT.value,
                    description=candidate.description,
                    dependencies=candidate.dependencies,
                    resource_estimates=(
                        candidate.resource_needs.model_dump(mode="json")
                        if candidate.resource_needs
                        else {}
                    ),
                    known_limitations=candidate.cons + candidate.risks,
                    required_permissions=["security.review"],
                    requirement_id=requirement_ids[0] if adopted else None,
                    objective_id=finding.objective_id,
                    correlation_id=message.correlation_id,
                )
                if adopted:
                    await capabilities.transition(
                        capability_id,
                        CapabilityStatus.PROPOSED,
                        actor=AgentId.SCOUT.value,
                        correlation_id=message.correlation_id,
                    )
        if requirement_ids:
            # SCOUT evaluated the evidence and adopted the approach: the requirement is approved
            # for implementation (operations still need WARDEN; completion needs LEDGER).
            for target in (AgentId.LEDGER, AgentId.ORCHESTR):
                await publish(
                    AgentId.SCOUT,
                    target,
                    RequirementEvaluatedPayload(
                        requirement_id=requirement_ids[0],
                        objective_id=finding.objective_id,
                        title=requirement_title,
                        requirement_type=RequirementType.CAPABILITY,
                        acceptance_criteria=criteria,
                        source_finding_id=finding.id,
                        security_review_required=True,
                        outcome=EvaluationOutcome.APPROVED,
                        rationale=decision.rationale,
                    ),
                    transaction,
                    key=finding.id,
                    suffix=f"evaluated:{target.value}",
                    correlation_id=message.correlation_id,
                )
        statuses = {
            ConsolidationDecision.ADOPT: FindingStatus.ADOPTED,
            ConsolidationDecision.REJECT: FindingStatus.REJECTED,
            ConsolidationDecision.NEEDS_EVIDENCE: FindingStatus.NEEDS_EVIDENCE,
            ConsolidationDecision.DELEGATE_ROGUE: FindingStatus.NEEDS_EVIDENCE,
        }
        await repository.set_status(
            finding.id,
            FindingStatus.CONSOLIDATED,
            actor=AgentId.SCOUT.value,
            rationale=decision.rationale,
            correlation_id=message.correlation_id,
        )
        await repository.set_status(
            finding.id,
            statuses[decision.decision],
            actor=AgentId.SCOUT.value,
            rationale=decision.rationale,
            correlation_id=message.correlation_id,
        )
        for target in (AgentId.ORCHESTR, AgentId.ROGUE, AgentId.LEDGER):
            await publish(
                AgentId.SCOUT,
                target,
                ResearchConsolidatedPayload(
                    objective_id=finding.objective_id,
                    summary=decision.rationale,
                    decisions=[decision],
                    requirement_ids=requirement_ids,
                ),
                transaction,
                key=finding.id,
                suffix=f"consolidated:{target.value}",
                correlation_id=message.correlation_id,
            )
        return decision
