"""MUSE's ORCHESTR-only communication worker and trusted user-input services."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.muse.repository import MuseRepository
from aether.agents.muse.schemas import FileWriteReceipt, ResponseCheckpoint, StoredOutbox
from aether.agents.muse.workflow import (
    active_person,
    feedback_preferences,
    handoff_options,
    personalization,
    polish_opening,
    render_message,
    require_permission,
    routine_notification,
)
from aether.agents.security_review import REVIEWABLE_REASONS
from aether.communication.contracts import (
    CommunicationDeliveredPayload,
    CommunicationRequestPayload,
    DecisionOutcome,
    ProgressUpdatePayload,
    SecurityDecisionPayload,
    SecurityEvaluatePayload,
    UserDecision,
    UserResponsePayload,
    build_message,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage
from aether.security.errors import BlockReason, OperationBlocked
from aether.storage.models import TaskRecord
from aether.tools import ExecutionFailed, ToolCallContext, ToolInputError, UnknownTool


@dataclass(frozen=True, slots=True)
class MuseSettings:
    data_dir: Path = Path("data")
    dedupe_window: timedelta = timedelta(minutes=5)
    polish: bool = False

    def __post_init__(self) -> None:
        if self.dedupe_window < timedelta(0):
            raise ValueError("MUSE dedupe window must not be negative")


class MuseAgent(BaseAgent):
    agent_id = AgentId.MUSE
    accepted_message_types = receivable_types(AgentId.MUSE)

    def __init__(self, settings: MuseSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or MuseSettings()

    def _settings(self, ctx: AgentContext) -> MuseSettings:
        settings = ctx.services.get("muse_settings", self.settings)
        if not isinstance(settings, MuseSettings):
            raise TypeError("muse_settings must be MuseSettings")
        return settings

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        self.ensure_accepts(message)
        payload = validate_message(message)
        await require_permission(ctx, "communication.prepare")
        if isinstance(payload, CommunicationRequestPayload):
            result = await self.communicate(message, ctx)
            if result.status is not TaskResultStatus.COMPLETED:
                await self._report_delivery_state(message, result, ctx)
        elif isinstance(payload, SecurityDecisionPayload):
            await MuseRepository(ctx.sessions).record_control(message)
            if payload.decision is DecisionOutcome.APPROVED:
                for result in await self.resume_pending(ctx, input_hash=payload.input_hash):
                    if result.status is not TaskResultStatus.COMPLETED:
                        await self._report_delivery_state(message, result, ctx)
        else:
            # Lifecycle/task/authorization notices are not ORCHESTR communication handoffs.
            await MuseRepository(ctx.sessions).record_control(message)

    async def communicate(self, message: AgentMessage, ctx: AgentContext) -> TaskResult:
        self.ensure_accepts(message)
        payload = validate_message(message)
        if not isinstance(payload, CommunicationRequestPayload):
            raise TypeError("Communication requires an ORCHESTR communication.request")
        await require_permission(ctx, "communication.prepare")
        request = payload.model_copy(
            update={
                "objective_id": payload.objective_id or message.objective_id,
                "task_id": payload.task_id or message.task_id,
            }
        )
        if request.audience is not None:
            await active_person(ctx, request.audience)
        repository = MuseRepository(ctx.sessions)
        found, stored = await repository.lookup_outbox(message.message_id)
        if not found:
            options = handoff_options(request)
            preferences, names = await personalization(request, options, ctx)
            canonical = render_message(
                request, preferences, options, deadline=message.deadline, context_names=names
            )
            settings = self._settings(ctx)
            rendered = canonical
            if settings.polish and not routine_notification(request, options):
                opening = await polish_opening(
                    preferences, ctx, objective_id=request.objective_id, task_id=request.task_id
                )
                rendered = render_message(
                    request,
                    preferences,
                    options,
                    deadline=message.deadline,
                    context_names=names,
                    opening=opening,
                )
            stored = await repository.prepare_outbox(
                message,
                request,
                rendered=rendered,
                canonical_text=canonical,
                channel=preferences.channel,
                data_dir=settings.data_dir.absolute(),
                now=ctx.clock(),
                dedupe_window=settings.dedupe_window,
                suppress=routine_notification(request, options),
            )
        if stored is None:
            return TaskResult(
                status=TaskResultStatus.COMPLETED, summary="Routine notification suppressed"
            )
        return await self._deliver(stored, ctx)

    async def _deliver(self, stored: StoredOutbox, ctx: AgentContext) -> TaskResult:
        saved = stored.checkpoint
        if saved.audience is not None:
            await active_person(ctx, saved.audience)
        call = ToolCallContext(
            objective_id=saved.objective_id,
            task_id=saved.task_id,
            requirement_id=saved.requirement_id,
            correlation_id=saved.correlation_id,
        )
        delivered_at = stored.delivered_at
        if delivered_at is None:
            if ctx.tools is None:
                return TaskResult(
                    status=TaskResultStatus.FAILED,
                    error="Tool facade is not configured",
                    summary="Outbox delivery is pending",
                    retryable=False,
                )
            if ctx.tools.agent_id is not AgentId.MUSE:
                raise PermissionError("MUSE cannot use another agent's tool facade")
            args = {"path": str(saved.path), "content": stored.text}
            try:
                result = await ctx.tools.invoke("deliver_outbox_message", args, call)
            except OperationBlocked as blocked:
                if blocked.reason in REVIEWABLE_REASONS:
                    operation = ctx.tools.build_operation("deliver_outbox_message", args, call)
                    await ctx.bus.publish(
                        build_message(
                            AgentId.MUSE,
                            AgentId.WARDEN,
                            SecurityEvaluatePayload(
                                operation=operation,
                                input_hash=operation.input_hash,
                                requested_by=AgentId.MUSE,
                                task_id=saved.task_id,
                                requirement_id=saved.requirement_id,
                                block_reason=blocked.reason,
                            ),
                            message_id=uuid5(
                                saved.interaction_id,
                                f"review:{operation.input_hash}:{blocked.reason}:{blocked.decision_id}",
                            ),
                            correlation_id=saved.correlation_id,
                            objective_id=saved.objective_id,
                            task_id=saved.task_id,
                            requirement_id=saved.requirement_id,
                            requires_response=True,
                        )
                    )
                waiting = blocked.reason in REVIEWABLE_REASONS or (
                    blocked.reason is BlockReason.USER_AUTHORIZATION_PENDING
                )
                return TaskResult(
                    status=(
                        TaskResultStatus.WAITING_FOR_APPROVAL
                        if waiting
                        else TaskResultStatus.BLOCKED
                    ),
                    summary="Outbox delivery is pending authorization"
                    if waiting
                    else "Outbox delivery blocked",
                    outputs={"block_reason": blocked.reason.value},
                    error=None if waiting else blocked.detail,
                    retryable=waiting,
                )
            except (ExecutionFailed, ToolInputError, UnknownTool, OSError) as error:
                ctx.logger.error(
                    "MUSE outbox tool failed", extra={"error_type": type(error).__name__}
                )
                return TaskResult(
                    status=TaskResultStatus.FAILED,
                    summary="Outbox delivery failed",
                    error=type(error).__name__,
                )
            receipt = FileWriteReceipt.model_validate(result, from_attributes=True)
            if (
                receipt.sha256 != saved.rendered_text_hash
                or receipt.bytes_written != len(stored.text.encode("utf-8"))
                or receipt.path != saved.path
            ):
                raise ValueError("Outbox tool did not confirm the exact message artifact")
            delivered_at = await MuseRepository(ctx.sessions).mark_delivered(stored, ctx.clock())
        payload = CommunicationDeliveredPayload(
            interaction_id=saved.interaction_id,
            channel=saved.channel,
            rendered_text_hash=saved.rendered_text_hash,
            audience=saved.audience,
            request_message_id=saved.request_message_id,
            delivered_at=delivered_at,
        )
        for target in (AgentId.ORCHESTR, AgentId.LEDGER):
            await ctx.bus.publish(
                build_message(
                    AgentId.MUSE,
                    target,
                    payload,
                    message_id=uuid5(saved.interaction_id, f"delivered:{target.value}"),
                    correlation_id=saved.correlation_id,
                    objective_id=saved.objective_id,
                    task_id=saved.task_id,
                    requirement_id=saved.requirement_id,
                )
            )
        return TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary="Communication delivered to the user outbox",
            outputs={
                "interaction_id": str(saved.interaction_id),
                "path": str(saved.path),
                "rendered_text_hash": saved.rendered_text_hash,
                "channel": saved.channel,
            },
        )

    async def _report_delivery_state(
        self, message: AgentMessage, result: TaskResult, ctx: AgentContext
    ) -> None:
        ctx.logger.warning("MUSE communication not delivered", extra={"state": result.status.value})
        objective_id, task_id = message.objective_id, message.task_id
        payload = validate_message(message)
        if isinstance(payload, CommunicationRequestPayload):
            objective_id = objective_id or payload.objective_id
            task_id = task_id or payload.task_id
        await ctx.bus.publish(
            build_message(
                AgentId.MUSE,
                AgentId.ORCHESTR,
                ProgressUpdatePayload(
                    objective_id=objective_id,
                    task_id=task_id,
                    summary=result.summary,
                    details={
                        "status": result.status.value,
                        "error": result.error,
                        **result.outputs,
                    },
                ),
                message_id=uuid5(message.message_id, f"delivery-state:{result.status.value}"),
                correlation_id=message.correlation_id,
                objective_id=objective_id,
                task_id=task_id,
                requirement_id=message.requirement_id,
            )
        )

    async def resume_pending(
        self, ctx: AgentContext, *, input_hash: str | None = None, task_id: UUID | None = None
    ) -> list[TaskResult]:
        await require_permission(ctx, "communication.prepare")
        repository = MuseRepository(ctx.sessions)
        results = []
        for stored in await repository.outboxes(task_id):
            saved = stored.checkpoint
            if input_hash is not None:
                if ctx.tools is None:
                    continue
                call = ToolCallContext(
                    task_id=saved.task_id,
                    objective_id=saved.objective_id,
                    requirement_id=saved.requirement_id,
                    correlation_id=saved.correlation_id,
                )
                operation = ctx.tools.build_operation(
                    "deliver_outbox_message",
                    {"path": str(saved.path), "content": stored.text},
                    call,
                )
                if operation.input_hash != input_hash:
                    continue
            results.append(await self._deliver(stored, ctx))
        for response in await repository.pending_responses():
            await _forward_response(response, ctx)
        return results

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        if task.assigned_agent != AgentId.MUSE.value:
            return TaskResult(
                status=TaskResultStatus.FAILED,
                summary="Task is not assigned to MUSE",
                retryable=False,
            )
        results = await self.resume_pending(ctx, task_id=task.id)
        if not results:
            return TaskResult(
                status=TaskResultStatus.BLOCKED,
                summary="Waiting for an ORCHESTR communication.request; task text is not a handoff",
                retryable=False,
            )
        return next(
            (result for result in results if result.status is not TaskResultStatus.COMPLETED),
            TaskResult(status=TaskResultStatus.COMPLETED, summary="Task communications delivered"),
        )


async def _forward_response(checkpoint: ResponseCheckpoint, ctx: AgentContext) -> None:
    response = checkpoint.response
    if checkpoint.person_id is not None:
        await active_person(ctx, checkpoint.person_id)
    targets = [AgentId.ORCHESTR]
    if response.authorization_request_id is not None:
        targets.append(AgentId.WARDEN)
    for target in targets:
        await ctx.bus.publish(
            build_message(
                AgentId.MUSE,
                target,
                response,
                message_id=uuid5(checkpoint.response_id, f"user.response:{target.value}"),
                correlation_id=checkpoint.correlation_id,
                objective_id=response.objective_id,
                task_id=response.task_id,
                requirement_id=checkpoint.requirement_id,
            )
        )
    await MuseRepository(ctx.sessions).mark_response_forwarded(checkpoint)


async def record_user_response(
    ctx: AgentContext,
    *,
    text: str,
    person_id: UUID | None = None,
    authorization_request_id: UUID | None = None,
    objective_id: UUID | None = None,
    task_id: UUID | None = None,
    requirement_id: UUID | None = None,
    decision: UserDecision = UserDecision.NONE,
    channel: str = "console",
    response_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> UUID:
    """Trusted CLI/API entry point; authenticate the caller before supplying person_id.

    Never infer approval from text. An explicit decision is forwarded to WARDEN, not applied
    by MUSE. Reuse response_id when retrying the same submitted response.
    """
    await require_permission(ctx, "communication.prepare")
    if not text.strip():
        raise ValueError("Silence is not a user response or authorization")
    repository = MuseRepository(ctx.sessions)
    response_id = response_id or uuid4()
    correlation_id = (
        correlation_id
        or await repository.response_correlation(
            person_id, authorization_request_id, objective_id, task_id, response_id
        )
        or uuid5(response_id, "muse.response.correlation")
    )
    now = ctx.clock()
    checkpoint = ResponseCheckpoint(
        response_id=response_id,
        interaction_id=uuid5(response_id, "muse.response"),
        person_id=person_id,
        channel=channel,
        correlation_id=correlation_id,
        requirement_id=requirement_id,
        response=UserResponsePayload(
            text=text,
            decision=decision,
            authorization_request_id=authorization_request_id,
            objective_id=objective_id,
            task_id=task_id,
            responded_at=now,
        ),
    )
    saved = await repository.record_response(checkpoint, now)
    await _forward_response(saved, ctx)
    return saved.interaction_id


async def record_feedback(
    ctx: AgentContext,
    *,
    person_id: UUID,
    text: str,
    feedback_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> None:
    await require_permission(ctx, "communication.prepare")
    await require_permission(ctx, "preferences.read")
    await MuseRepository(ctx.sessions).record_feedback(
        person_id,
        feedback_id or uuid4(),
        feedback_preferences(text),
        text=text,
        correlation_id=correlation_id or uuid4(),
        now=ctx.clock(),
    )


async def delete_person_data(
    ctx: AgentContext, *, person_id: UUID, correlation_id: UUID | None = None
) -> None:
    """Trusted, explicit privacy deletion request; not available through inter-agent payloads."""
    await require_permission(ctx, "communication.prepare")
    await MuseRepository(ctx.sessions).delete_person_data(
        person_id, "MUSE", correlation_id=correlation_id or uuid4(), now=ctx.clock()
    )
