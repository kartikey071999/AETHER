"""Structured security failures raised by the enforcement layer."""

from enum import StrEnum
from uuid import UUID


class PolicyMutationError(AttributeError):
    """Raised on any attempt to modify the immutable runtime security policy."""


class UnauthorizedDecisionMaker(PermissionError):
    """Only WARDEN may record security decisions."""


class InvalidSecurityDecision(ValueError):
    """A security decision is malformed (for example an approval without a valid expiry)."""


class BlockReason(StrEnum):
    AGENT_NOT_PERMITTED = "agent_not_permitted"
    POLICY_VIOLATION = "policy_violation"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_NEEDS_EVIDENCE = "approval_needs_evidence"
    APPROVAL_EXPIRED = "approval_expired"
    VERSION_MISMATCH = "version_mismatch"
    USER_AUTHORIZATION_PENDING = "user_authorization_pending"
    USER_AUTHORIZATION_DENIED = "user_authorization_denied"
    INVALID_AUTHORIZATION = "invalid_authorization"


class OperationBlocked(PermissionError):
    """The enforcement layer refused an operation; the refusal is durably recorded."""

    def __init__(
        self,
        reason: BlockReason,
        detail: str,
        *,
        operation: str,
        input_hash: str,
        decision_id: UUID | None = None,
        user_request_id: UUID | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.operation = operation
        self.input_hash = input_hash
        self.decision_id = decision_id
        self.user_request_id = user_request_id
        super().__init__(f"{operation} blocked ({reason.value}): {detail}")
