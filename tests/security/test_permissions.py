import sys
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.security import (
    ApprovalService,
    BlockReason,
    DecisionOutcome,
    InvalidSecurityDecision,
    Operation,
    OperationBlocked,
    OperationKind,
    PermissionService,
    UnauthorizedDecisionMaker,
)
from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    SecurityDecisionRecord,
)
from aether.storage.repositories import AuthorizationRequestRepository, SecurityDecisionRepository


def subprocess_operation(*argv: str, agent: AgentId = AgentId.FORGE, **extra) -> Operation:
    return Operation(
        kind=OperationKind.SUBPROCESS,
        target=sys.executable,
        arguments={"argv": list(argv) or ["-V"], "timeout_seconds": None, **extra},
        cwd=".",
        requested_by=agent,
    )


async def test_agent_without_declared_permission_is_blocked(permissions, events_of) -> None:
    operation = subprocess_operation(agent=AgentId.MUSE)
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.AGENT_NOT_PERMITTED
    blocked = await events_of("security.operation_blocked")
    assert len(blocked) == 1
    assert blocked[0].aggregate_id == operation.aggregate_id
    assert blocked[0].actor == "MUSE"
    assert blocked[0].payload["reason"].startswith("agent_not_permitted")


async def test_non_allowlisted_executable_is_blocked_before_approval(permissions, approve) -> None:
    operation = subprocess_operation().model_copy(update={"target": "definitely-not-a-tool"})
    await approve(operation)
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    assert "executable" in raised.value.detail


async def test_non_allowlisted_host_and_path_traversal_are_blocked(permissions) -> None:
    http = Operation(
        kind=OperationKind.HTTP_REQUEST,
        target="https://evil.example.net/payload",
        requested_by=AgentId.SCOUT,
    )
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(http)
    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    traversal = Operation(
        kind=OperationKind.FILE_READ,
        target="..\\..\\outside\\secrets.txt",
        requested_by=AgentId.FORGE,
    )
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(traversal)
    assert raised.value.reason is BlockReason.POLICY_VIOLATION
    escaping_cwd = subprocess_operation().model_copy(update={"cwd": "../.."})
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(escaping_cwd)
    assert "working directory" in raised.value.detail


async def test_missing_approval_blocks(permissions) -> None:
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(subprocess_operation())
    assert raised.value.reason is BlockReason.APPROVAL_MISSING


async def test_approval_authorizes_only_the_exact_input(permissions, approve) -> None:
    approved = subprocess_operation("-c", "print('ok')")
    decision = await approve(approved)
    grant = await permissions.authorize(approved)
    assert grant.input_hash == approved.input_hash
    assert grant.decision_id == decision.decision_id

    modified = subprocess_operation("-c", "print('ok'); import os")
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(modified)
    assert raised.value.reason is BlockReason.APPROVAL_MISSING

    other_agent = subprocess_operation("-c", "print('ok')", agent=AgentId.LEDGER)
    with pytest.raises(OperationBlocked):
        await permissions.authorize(other_agent)


async def test_expired_approval_is_rejected(permissions, approve, clock) -> None:
    operation = subprocess_operation()
    await approve(operation, expires_at=clock.now + timedelta(minutes=5))
    await permissions.authorize(operation)
    clock.advance(timedelta(minutes=5, seconds=1))
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.APPROVAL_EXPIRED


async def test_authorization_never_outlives_its_approval(permissions, approve, clock) -> None:
    operation = subprocess_operation()
    decision = await approve(operation, expires_at=clock.now + timedelta(seconds=10))
    grant = await permissions.authorize(operation)
    assert grant.expires_at == decision.expires_at


async def test_rejection_and_needs_evidence_block_and_latest_decision_wins(
    permissions, approve, clock
) -> None:
    operation = subprocess_operation()
    await approve(operation, DecisionOutcome.NEEDS_EVIDENCE)
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.APPROVAL_NEEDS_EVIDENCE

    clock.advance(timedelta(seconds=1))
    await approve(operation)
    await permissions.authorize(operation)

    clock.advance(timedelta(seconds=1))
    rejection = await approve(operation, DecisionOutcome.REJECTED)
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.APPROVAL_REJECTED
    assert raised.value.decision_id == rejection.decision_id


async def test_version_constraint_is_enforced(permissions, approve) -> None:
    pinned = Operation(
        kind=OperationKind.PACKAGE_INSTALL,
        target="example-package",
        arguments={"version": "1.2.3"},
        requested_by=AgentId.FORGE,
    )
    await approve(pinned, version_constraint="1.2.4")
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(pinned)
    assert raised.value.reason is BlockReason.VERSION_MISMATCH


async def test_kinds_without_review_requirement_are_allowed_without_approval(
    permissions, roots
) -> None:
    read = Operation(kind=OperationKind.FILE_READ, target="notes.txt", requested_by=AgentId.LEDGER)
    grant = await permissions.authorize(read)
    assert grant.decision_id is None


async def test_only_warden_can_record_decisions(approvals: ApprovalService, database) -> None:
    operation = subprocess_operation()
    for agent in (AgentId.FORGE, AgentId.ORCHESTR, "warden", "WARDEN-impostor"):
        with pytest.raises(UnauthorizedDecisionMaker):
            await approvals.record_decision(
                agent, operation, DecisionOutcome.APPROVED, rationale="I approve myself"
            )
    async with database.sessions() as session:
        assert list(await session.scalars(select(SecurityDecisionRecord))) == []


async def test_decision_validation(approvals: ApprovalService, clock) -> None:
    operation = subprocess_operation()
    with pytest.raises(InvalidSecurityDecision):
        await approvals.record_decision(
            AgentId.WARDEN, operation, DecisionOutcome.APPROVED, rationale="  "
        )
    with pytest.raises(InvalidSecurityDecision):
        await approvals.record_decision(
            AgentId.WARDEN,
            operation,
            DecisionOutcome.APPROVED,
            rationale="too long",
            expires_at=clock.now + timedelta(days=2),
        )
    with pytest.raises(InvalidSecurityDecision):
        await approvals.record_decision(
            AgentId.WARDEN,
            operation,
            DecisionOutcome.APPROVED,
            rationale="already expired",
            expires_at=clock.now - timedelta(seconds=1),
        )


async def test_decision_is_durably_recorded_with_event(approve, database, events_of, clock) -> None:
    operation = subprocess_operation()
    decision = await approve(
        operation, restrictions=["no network"], risks=["arbitrary code"], evidence=[{"a": 1}]
    )
    record = await SecurityDecisionRepository(database.sessions).get(decision.decision_id)
    assert record is not None
    assert record.input_hash == operation.input_hash
    assert record.decision == "approved"
    assert record.scope["decided_by"] == "WARDEN"
    assert record.scope["restrictions"] == ["no network"]
    assert "not proof of absolute safety" in record.scope["clearance_note"]
    assert record.evidence == [{"a": 1}]
    events = await events_of("security.decision_recorded")
    assert len(events) == 1
    assert events[0].aggregate_id == decision.decision_id
    assert events[0].actor == "WARDEN"
    assert events[0].payload["decision"] == "approved"


async def test_forged_decision_rows_are_not_trusted(permissions, database, clock) -> None:
    operation = subprocess_operation()
    async with database.sessions() as session, session.begin():
        await SecurityDecisionRepository.stage(
            session,
            decision="approved",
            operation=operation.describe(),
            scope={"decided_by": "FORGE"},
            evidence=[],
            rationale="self-approved",
            input_hash=operation.input_hash,
            expires_at=clock.now + timedelta(minutes=5),
            created_at=clock.now,
        )
    with pytest.raises(OperationBlocked) as raised:
        await permissions.authorize(operation)
    assert raised.value.reason is BlockReason.APPROVAL_MISSING


async def test_allowed_operations_are_recorded(permissions, approve, events_of) -> None:
    operation = subprocess_operation()
    await approve(operation)
    grant = await permissions.authorize(operation)
    events = await events_of("security.operation_authorized")
    assert len(events) == 1
    data = events[0].payload
    assert data["input_hash"] == operation.input_hash
    assert UUID(data["grant_id"]) == grant.grant_id


async def test_grants_are_single_use_and_operation_bound(permissions, approve) -> None:
    operation = subprocess_operation()
    await approve(operation)
    grant = await permissions.authorize(operation)
    assert await permissions.redeem(grant, operation) == grant
    with pytest.raises(OperationBlocked) as raised:
        await permissions.redeem(grant, operation)
    assert raised.value.reason is BlockReason.INVALID_AUTHORIZATION

    second = await permissions.authorize(operation)
    tampered = subprocess_operation("-c", "import shutil")
    with pytest.raises(OperationBlocked):
        await permissions.redeem(second, tampered)


async def test_forged_or_expired_grants_are_refused(permissions, approve, clock) -> None:
    operation = subprocess_operation()
    await approve(operation)
    grant = await permissions.authorize(operation)
    forged = grant.model_copy(update={"expires_at": grant.expires_at + timedelta(days=1)})
    with pytest.raises(OperationBlocked):
        await permissions.redeem(forged, operation)

    grant = await permissions.authorize(operation)
    clock.advance(timedelta(seconds=61))
    with pytest.raises(OperationBlocked) as raised:
        await permissions.redeem(grant, operation)
    assert "expired" in raised.value.detail
    with pytest.raises(OperationBlocked):
        await permissions.redeem(None, operation)


@pytest.fixture
def user_gated(database, recorder, policy_factory, clock) -> PermissionService:
    policy = policy_factory(user_authorization_required_kinds=[OperationKind.SUBPROCESS])
    return PermissionService(database.sessions, recorder, policy, clock=clock)


async def test_user_authorization_silence_never_authorizes(
    user_gated, approve, database, events_of, clock
) -> None:
    operation = subprocess_operation()
    decision = await approve(operation)
    with pytest.raises(OperationBlocked) as first:
        await user_gated.authorize(operation)
    assert first.value.reason is BlockReason.USER_AUTHORIZATION_PENDING
    request_id = first.value.user_request_id
    assert request_id is not None

    clock.advance(timedelta(minutes=10))
    with pytest.raises(OperationBlocked) as again:
        await user_gated.authorize(operation)
    assert again.value.reason is BlockReason.USER_AUTHORIZATION_PENDING
    assert again.value.user_request_id == request_id

    async with database.sessions() as session:
        request = await session.get(AuthorizationRequestRecord, request_id)
    assert request is not None
    assert request.status == AuthorizationStatus.PENDING
    assert request.security_decision_id == decision.decision_id
    assert operation.input_hash[:16] in request.question
    assert len(await events_of("security.user_authorization_requested")) == 1

    status = await AuthorizationRequestRepository(database.sessions).respond(
        request_id, approved=True, actor="user", now=clock.now
    )
    assert status is AuthorizationStatus.APPROVED
    grant = await user_gated.authorize(operation)
    assert grant.user_request_id == request_id
    assert grant.decision_id == decision.decision_id


async def test_user_denial_blocks(user_gated, approve, database, clock) -> None:
    operation = subprocess_operation()
    await approve(operation)
    with pytest.raises(OperationBlocked) as pending:
        await user_gated.authorize(operation)
    await AuthorizationRequestRepository(database.sessions).respond(
        pending.value.user_request_id, approved=False, actor="user", now=clock.now
    )
    with pytest.raises(OperationBlocked) as raised:
        await user_gated.authorize(operation)
    assert raised.value.reason is BlockReason.USER_AUTHORIZATION_DENIED


async def test_user_authorization_does_not_transfer_to_modified_operation(
    user_gated, approve, database, clock
) -> None:
    operation = subprocess_operation()
    await approve(operation)
    with pytest.raises(OperationBlocked) as pending:
        await user_gated.authorize(operation)
    await AuthorizationRequestRepository(database.sessions).respond(
        pending.value.user_request_id, approved=True, actor="user", now=clock.now
    )
    modified = subprocess_operation("-c", "print('different')")
    with pytest.raises(OperationBlocked) as raised:
        await user_gated.authorize(modified)
    assert raised.value.reason is BlockReason.APPROVAL_MISSING


async def test_is_allowed_uses_declared_agent_actions(permissions) -> None:
    assert await permissions.is_allowed(AgentId.FORGE, "execution.request")
    assert not await permissions.is_allowed(AgentId.MUSE, "execution.request")
    assert await permissions.is_allowed(AgentId.WARDEN, "authorization.decide")
