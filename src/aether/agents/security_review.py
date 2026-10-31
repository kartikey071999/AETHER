"""Reusable, LLM-free routing of blocked operations to WARDEN for review."""

from uuid import UUID

from aether.agents.base import AgentContext, TaskResult, TaskResultStatus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.errors import BlockReason, OperationBlocked
from aether.security.operations import Operation

# Blocks WARDEN can resolve by (re-)reviewing the exact operation.
REVIEWABLE_REASONS = frozenset(
    {
        BlockReason.APPROVAL_MISSING,
        BlockReason.APPROVAL_EXPIRED,
        BlockReason.APPROVAL_NEEDS_EVIDENCE,
        BlockReason.VERSION_MISMATCH,
    }
)


def _outputs(operation: Operation, reason: BlockReason) -> dict[str, str]:
    return {
        "operation": operation.describe(),
        "kind": operation.kind.value,
        "input_hash": operation.input_hash,
        "block_reason": reason.value,
    }


async def request_security_review(
    operation: Operation,
    ctx: AgentContext,
    *,
    reason: BlockReason = BlockReason.APPROVAL_MISSING,
    correlation_id: UUID | None = None,
) -> TaskResult:
    """Ask WARDEN to review ``operation`` and report the task as waiting for approval.

    The message carries the full ``Operation`` and its ``input_hash`` so WARDEN's decision binds
    to exactly this input. WARDEN cannot approve its own operations. A supplied
    ``correlation_id`` is reused so the review stays in the caller's workflow trace.
    """
    if operation.requested_by is AgentId.WARDEN:
        return TaskResult(
            status=TaskResultStatus.BLOCKED,
            summary="WARDEN cannot approve its own operation",
            outputs=_outputs(operation, reason),
            error="self-approval is not permitted",
            retryable=False,
        )
    correlation = {} if correlation_id is None else {"correlation_id": correlation_id}
    message = AgentMessage(
        source_agent=operation.requested_by,
        target_agent=AgentId.WARDEN,
        message_type=MessageType.SECURITY_EVALUATE,
        objective_id=operation.objective_id,
        task_id=operation.task_id,
        requirement_id=operation.declared_requirement_id,
        **correlation,
        payload={
            "operation": operation.model_dump(mode="json"),
            "input_hash": operation.input_hash,
            "block_reason": reason.value,
        },
        requires_response=True,
    )
    await ctx.bus.publish(message)
    return TaskResult(
        status=TaskResultStatus.WAITING_FOR_APPROVAL,
        summary=f"waiting for WARDEN review of {operation.describe()}",
        outputs={**_outputs(operation, reason), "review_message_id": str(message.message_id)},
    )


async def blocked_result(
    blocked: OperationBlocked,
    operation: Operation,
    ctx: AgentContext,
    *,
    correlation_id: UUID | None = None,
) -> TaskResult:
    """Structured result for an `OperationBlocked`: review, wait on the user, or stay blocked."""
    if blocked.reason in REVIEWABLE_REASONS:
        return await request_security_review(
            operation, ctx, reason=blocked.reason, correlation_id=correlation_id
        )
    outputs = _outputs(operation, blocked.reason)
    if blocked.user_request_id is not None:
        outputs["user_request_id"] = str(blocked.user_request_id)
    if blocked.reason is BlockReason.USER_AUTHORIZATION_PENDING:
        return TaskResult(
            status=TaskResultStatus.WAITING_FOR_APPROVAL,
            summary="waiting for explicit user authorization",
            outputs=outputs,
        )
    return TaskResult(
        status=TaskResultStatus.BLOCKED,
        summary=f"operation blocked: {blocked.reason.value}",
        outputs=outputs,
        error=blocked.detail,
        retryable=False,
    )
