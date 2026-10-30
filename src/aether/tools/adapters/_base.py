"""Defence-in-depth checks shared by every privileged adapter."""

from uuid import UUID

from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService


class ExecutionFailed(RuntimeError):
    """An authorized operation started but could not complete; the failure is recorded."""


async def admit(
    permissions: PermissionService,
    kinds: frozenset[OperationKind],
    operation: Operation,
    authorization: Authorization | None,
    *,
    correlation_id: UUID | None = None,
) -> tuple[Operation, Authorization]:
    """Consume the grant for an immutable snapshot of ``operation`` and re-check static policy.

    Adapters must act only on the returned snapshot, so later changes to the caller's object
    cannot alter what runs.
    """
    snapshot = operation.model_copy(deep=True)
    grant = await permissions.redeem(authorization, snapshot, correlation_id=correlation_id)
    if snapshot.kind not in kinds:
        raise await permissions.reject(
            snapshot,
            BlockReason.POLICY_VIOLATION,
            f"adapter does not perform {snapshot.kind.value}",
            decision_id=grant.decision_id,
            correlation_id=correlation_id,
        )
    violation = permissions.policy.static_violation(snapshot)
    if violation is not None:
        raise await permissions.reject(
            snapshot,
            BlockReason.POLICY_VIOLATION,
            violation,
            decision_id=grant.decision_id,
            correlation_id=correlation_id,
        )
    return snapshot, grant
