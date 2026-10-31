"""Central permission enforcement: every privileged operation is authorized here first.

Decisions are made from the immutable ``SecurityPolicy`` and durable WARDEN/user records,
never from LLM output. Every allow and every block is written as a durable event.
"""

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.registry import get_agent
from aether.communication.messages import AgentId
from aether.observability.events import AetherEvent
from aether.observability.recorder import EventRecorder
from aether.security._audit import log_security, stage_event
from aether.security.errors import BlockReason, InvalidSecurityDecision, OperationBlocked
from aether.security.operations import Operation, OperationKind, normalize_host
from aether.security.policy import SecurityPolicy
from aether.storage.models import AuthorizationStatus, SecurityDecisionRecord
from aether.storage.repositories._events import as_utc
from aether.storage.repositories.security import (
    OperationAuthorizationRequests,
    SecurityDecisionRepository,
)


class ExecutionRestrictions(BaseModel):
    """Validated decision limits; absent values leave immutable policy limits in place."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_runtime_seconds: float | None = None
    no_network: bool = False
    read_only: bool = False
    max_output_bytes: int | None = None
    allowed_hosts: tuple[str, ...] | None = None


def parse_restrictions(restrictions: object) -> ExecutionRestrictions:
    """Parse flags and ``name=value`` limits. Unknown or ambiguous restrictions fail closed."""
    if not isinstance(restrictions, list | tuple):
        raise InvalidSecurityDecision("restrictions must be a list of strings")
    values: dict[str, object] = {}
    for item in restrictions:
        if not isinstance(item, str):
            raise InvalidSecurityDecision("restrictions must contain strings")
        name, separator, value = item.partition("=")
        if name in values:
            raise InvalidSecurityDecision(f"duplicate restriction: {name}")
        if name in {"no_network", "read_only"} and not separator:
            values[name] = True
        elif name == "max_runtime_seconds" and separator:
            try:
                seconds = float(value)
            except ValueError as error:
                raise InvalidSecurityDecision("max_runtime_seconds must be numeric") from error
            if not math.isfinite(seconds) or seconds <= 0:
                raise InvalidSecurityDecision("max_runtime_seconds must be positive and finite")
            values[name] = seconds
        elif name == "max_output_bytes" and separator:
            try:
                size = int(value) if value.isascii() and value.isdecimal() else 0
            except ValueError as error:
                raise InvalidSecurityDecision("max_output_bytes is too large") from error
            if size <= 0:
                raise InvalidSecurityDecision("max_output_bytes must be a positive integer")
            values[name] = size
        elif name == "allowed_hosts" and separator:
            hosts = tuple(normalize_host(host) for host in value.split(","))
            if any(not host or any(char in host for char in "/\\@*?# \t\r\n=") for host in hosts):
                raise InvalidSecurityDecision("allowed_hosts requires comma-separated exact hosts")
            values[name] = tuple(sorted(set(hosts)))
        else:
            raise InvalidSecurityDecision(f"unknown or malformed restriction: {name}")
    return ExecutionRestrictions.model_validate(values)


class Authorization(BaseModel):
    """Short-lived, single-use grant bound to one operation hash. Only issued here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: UUID
    input_hash: str
    kind: OperationKind
    requested_by: AgentId
    issued_at: datetime
    expires_at: datetime
    decision_id: UUID | None = None
    user_request_id: UUID | None = None
    restrictions: tuple[str, ...] = ()

    @property
    def limits(self) -> ExecutionRestrictions:
        return parse_restrictions(self.restrictions)


@dataclass(frozen=True, slots=True)
class _Block:
    reason: BlockReason
    detail: str
    decision_id: UUID | None = None
    user_request_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Allow:
    decision_id: UUID | None = None
    user_request_id: UUID | None = None
    expires_at: datetime | None = None
    restrictions: tuple[str, ...] = ()


class PermissionService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        recorder: EventRecorder,
        policy: SecurityPolicy,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._recorder = recorder
        self._policy = policy
        self._clock = clock
        self._grants: dict[UUID, Authorization] = {}
        self._lock = threading.Lock()

    @property
    def policy(self) -> SecurityPolicy:
        return self._policy

    async def is_allowed(self, agent_id: AgentId, action: str) -> bool:
        """Whether ``action`` is a declared permission of ``agent_id`` (agents/registry.py)."""
        return action in get_agent(AgentId(agent_id)).allowed_actions

    async def authorize(
        self, operation: Operation, *, correlation_id: UUID | None = None
    ) -> Authorization:
        """Return a single-use ``Authorization`` or raise a durably recorded ``OperationBlocked``."""
        operation = operation.model_copy(deep=True)
        now = self._clock()
        async with self._sessions() as session, session.begin():
            outcome = await self._evaluate(session, operation, now, correlation_id)
            if isinstance(outcome, _Block):
                event = self._stage_block(session, operation, outcome, correlation_id)
            else:
                expires_at = now + self._policy.authorization_ttl
                if outcome.expires_at is not None:
                    expires_at = min(expires_at, outcome.expires_at)
                grant = Authorization(
                    grant_id=uuid4(),
                    input_hash=operation.input_hash,
                    kind=operation.kind,
                    requested_by=operation.requested_by,
                    issued_at=now,
                    expires_at=expires_at,
                    decision_id=outcome.decision_id,
                    user_request_id=outcome.user_request_id,
                    restrictions=outcome.restrictions,
                )
                event = stage_event(
                    session,
                    self._recorder,
                    "security.operation_authorized",
                    actor=operation.requested_by.value,
                    aggregate_type="operation",
                    aggregate_id=operation.aggregate_id,
                    payload={
                        "operation": operation.describe(),
                        "kind": operation.kind.value,
                        "input_hash": grant.input_hash,
                        "grant_id": grant.grant_id,
                        "expires_at": grant.expires_at,
                        "decision_id": grant.decision_id,
                        "user_request_id": grant.user_request_id,
                    },
                    objective_id=operation.objective_id,
                    task_id=operation.task_id,
                    correlation_id=correlation_id,
                )
        if isinstance(outcome, _Block):
            raise self._blocked(operation, outcome, event)
        # Register only after commit so a failed audit write never leaves a usable grant.
        with self._lock:
            self._prune(now)
            self._grants[grant.grant_id] = grant
        log_security(event, logging.INFO, input_hash=grant.input_hash, kind=grant.kind.value)
        return grant

    async def redeem(
        self,
        authorization: Authorization | None,
        operation: Operation,
        *,
        correlation_id: UUID | None = None,
    ) -> Authorization:
        """Consume a grant for exactly ``operation``. Adapters call this before acting."""
        if authorization is None:
            raise await self.reject(
                operation,
                BlockReason.INVALID_AUTHORIZATION,
                "no authorization was supplied",
                correlation_id=correlation_id,
            )
        with self._lock:
            # Popping first makes every grant single-use, even when the check below fails.
            grant = self._grants.pop(authorization.grant_id, None)
        problem: str | None = None
        if grant is None:
            problem = "authorization is unknown or was already used"
        elif grant != authorization:
            problem = "authorization does not match the issued grant"
        elif (
            grant.input_hash != operation.input_hash
            or grant.kind is not operation.kind
            or grant.requested_by is not operation.requested_by
        ):
            problem = "authorization is bound to a different operation"
        elif grant.expires_at <= self._clock():
            problem = "authorization has expired"
        if problem is not None or grant is None:
            raise await self.reject(
                operation,
                BlockReason.INVALID_AUTHORIZATION,
                problem or "authorization is invalid",
                correlation_id=correlation_id,
            )
        return grant

    async def reject(
        self,
        operation: Operation,
        reason: BlockReason,
        detail: str,
        *,
        decision_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> OperationBlocked:
        """Durably record a block (e.g. an adapter's defence-in-depth refusal) and return it."""
        outcome = _Block(reason, detail, decision_id=decision_id)
        async with self._sessions() as session, session.begin():
            event = self._stage_block(session, operation, outcome, correlation_id)
        return self._blocked(operation, outcome, event)

    async def _evaluate(
        self,
        session: AsyncSession,
        operation: Operation,
        now: datetime,
        correlation_id: UUID | None,
    ) -> _Block | _Allow:
        policy = self._policy
        if not policy.permits_operation(operation):
            return _Block(
                BlockReason.AGENT_NOT_PERMITTED,
                f"{operation.requested_by.value} has no declared permission for "
                f"{operation.kind.value}",
            )
        violation = policy.static_violation(operation)
        if violation is not None:
            return _Block(BlockReason.POLICY_VIOLATION, violation)
        decision = await SecurityDecisionRepository.latest_for_input_hash(
            session, operation.input_hash
        )
        if decision is None and not policy.requires_approval(operation.kind):
            return _Allow()
        checked = _check_decision(decision, operation, now)
        if isinstance(checked, _Block):
            return checked
        limits = parse_restrictions(checked.restrictions)
        if limits.allowed_hosts is not None and any(
            not policy.host_allowed_for(operation.kind, host) for host in limits.allowed_hosts
        ):
            return _Block(
                BlockReason.POLICY_VIOLATION,
                "allowed_hosts restriction must be a subset of the policy allowlist",
                checked.decision_id,
            )
        if policy.requires_user_authorization(operation.kind):
            user = await self._check_user_authorization(
                session, operation, checked, now, correlation_id
            )
            if isinstance(user, _Block):
                return user
            return _Allow(
                decision_id=checked.decision_id,
                user_request_id=user,
                expires_at=checked.expires_at,
                restrictions=checked.restrictions,
            )
        return checked

    async def _check_user_authorization(
        self,
        session: AsyncSession,
        operation: Operation,
        approval: _Allow,
        now: datetime,
        correlation_id: UUID | None,
    ) -> _Block | UUID:
        if approval.decision_id is None:
            return _Block(
                BlockReason.APPROVAL_MISSING, "user authorization needs a WARDEN decision"
            )
        request = await OperationAuthorizationRequests.latest_for_decision(
            session, approval.decision_id
        )
        if request is not None:
            expires_at = as_utc(request.expires_at)
            live = expires_at is None or expires_at > now
            if request.status == AuthorizationStatus.APPROVED and live:
                return request.id
            if request.status == AuthorizationStatus.PENDING and live:
                return _Block(
                    BlockReason.USER_AUTHORIZATION_PENDING,
                    "waiting for explicit user authorization",
                    decision_id=approval.decision_id,
                    user_request_id=request.id,
                )
            if request.status == AuthorizationStatus.DENIED:
                return _Block(
                    BlockReason.USER_AUTHORIZATION_DENIED,
                    "the user denied this operation",
                    decision_id=approval.decision_id,
                    user_request_id=request.id,
                )
        question = (
            f"{operation.requested_by.value} requests {operation.describe()} "
            f"(input sha256 {operation.input_hash[:16]}). Authorize?"
        )
        created = await OperationAuthorizationRequests.stage_pending(
            session,
            question=question,
            security_decision_id=approval.decision_id,
            requested_at=now,
            expires_at=approval.expires_at,
            objective_id=operation.objective_id,
            task_id=operation.task_id,
            input_hash=operation.input_hash,
        )
        stage_event(
            session,
            self._recorder,
            "security.user_authorization_requested",
            actor=operation.requested_by.value,
            aggregate_type="authorization_request",
            aggregate_id=created.id,
            payload={
                "operation": operation.describe(),
                "input_hash": operation.input_hash,
                "request_id": created.id,
                "decision_id": approval.decision_id,
                "question": question,
            },
            objective_id=operation.objective_id,
            task_id=operation.task_id,
            correlation_id=correlation_id,
        )
        return _Block(
            BlockReason.USER_AUTHORIZATION_PENDING,
            "explicit user authorization requested; silence never authorizes",
            decision_id=approval.decision_id,
            user_request_id=created.id,
        )

    def _stage_block(
        self,
        session: AsyncSession,
        operation: Operation,
        outcome: _Block,
        correlation_id: UUID | None,
    ) -> AetherEvent:
        return stage_event(
            session,
            self._recorder,
            "security.operation_blocked",
            actor=operation.requested_by.value,
            aggregate_type="operation",
            aggregate_id=operation.aggregate_id,
            payload={
                "operation": operation.describe(),
                "reason": f"{outcome.reason.value}: {outcome.detail}",
                "decision_id": outcome.decision_id,
                "input_hash": operation.input_hash,
            },
            objective_id=operation.objective_id,
            task_id=operation.task_id,
            correlation_id=correlation_id,
        )

    def _blocked(
        self, operation: Operation, outcome: _Block, event: AetherEvent
    ) -> OperationBlocked:
        log_security(
            event,
            logging.WARNING,
            input_hash=operation.input_hash,
            kind=operation.kind.value,
            block_reason=outcome.reason.value,
        )
        return OperationBlocked(
            outcome.reason,
            outcome.detail,
            operation=operation.describe(),
            input_hash=operation.input_hash,
            decision_id=outcome.decision_id,
            user_request_id=outcome.user_request_id,
        )

    def _prune(self, now: datetime) -> None:
        expired = [key for key, grant in self._grants.items() if grant.expires_at <= now]
        for key in expired:
            del self._grants[key]


def _check_decision(
    decision: SecurityDecisionRecord | None, operation: Operation, now: datetime
) -> _Block | _Allow:
    if decision is None or decision.scope.get("decided_by") != AgentId.WARDEN.value:
        return _Block(BlockReason.APPROVAL_MISSING, "no WARDEN decision covers this exact input")
    if decision.decision == "rejected":
        return _Block(BlockReason.APPROVAL_REJECTED, "WARDEN rejected this operation", decision.id)
    if decision.decision == "needs_evidence":
        return _Block(
            BlockReason.APPROVAL_NEEDS_EVIDENCE, "WARDEN requires more evidence", decision.id
        )
    if decision.decision != "approved":
        return _Block(BlockReason.APPROVAL_MISSING, "no WARDEN approval on record", decision.id)
    expires_at = as_utc(decision.expires_at)
    if expires_at is None or expires_at <= now:
        return _Block(BlockReason.APPROVAL_EXPIRED, "the WARDEN approval has expired", decision.id)
    constraint = decision.scope.get("version_constraint")
    if constraint is not None and operation.arguments.get("version") != constraint:
        return _Block(
            BlockReason.VERSION_MISMATCH,
            "operation version does not match the approved version",
            decision.id,
        )
    restrictions = decision.scope.get("restrictions", ())
    try:
        parse_restrictions(restrictions)
    except InvalidSecurityDecision as error:
        return _Block(BlockReason.POLICY_VIOLATION, str(error), decision.id)
    return _Allow(
        decision_id=decision.id,
        expires_at=expires_at,
        restrictions=tuple(restrictions),
    )
