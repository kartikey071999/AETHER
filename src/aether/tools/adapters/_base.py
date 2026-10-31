"""Defence-in-depth checks shared by every privileged adapter."""

from uuid import UUID

from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import NETWORK_KINDS, url_host


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
    limits = grant.limits
    hosts = set(snapshot.hosts)
    if snapshot.kind in NETWORK_KINDS and (host := url_host(snapshot.target)) is not None:
        hosts.add(host)
    if limits.no_network and (snapshot.kind in NETWORK_KINDS or hosts):
        violation = "no_network restriction forbids this network operation"
    if limits.read_only and snapshot.kind is OperationKind.FILE_WRITE:
        violation = "read_only restriction forbids file writes"
    if limits.allowed_hosts is not None and not hosts.issubset(limits.allowed_hosts):
        violation = "operation host is outside the allowed_hosts restriction"
    if limits.allowed_hosts is not None and snapshot.kind is OperationKind.SUBPROCESS:
        violation = "allowed_hosts cannot be enforced for subprocess network access"
    if violation is not None:
        raise await permissions.reject(
            snapshot,
            BlockReason.POLICY_VIOLATION,
            violation,
            decision_id=grant.decision_id,
            correlation_id=correlation_id,
        )
    return snapshot, grant
