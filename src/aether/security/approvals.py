"""WARDEN security decisions bound to the exact reviewed operation input."""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId
from aether.observability.events import EventCategory
from aether.observability.logging import get_logger
from aether.observability.recorder import EventRecorder
from aether.security._audit import log_security, stage_event
from aether.security.errors import InvalidSecurityDecision, UnauthorizedDecisionMaker
from aether.security.operations import Operation
from aether.security.policy import SecurityPolicy
from aether.security.untrusted import UntrustedContent
from aether.storage.repositories._events import to_json
from aether.storage.repositories.security import SecurityDecisionRepository

CLEARANCE_NOTE = (
    "Clearance under AETHER's defined checks and policies; not proof of absolute safety."
)


class DecisionOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_EVIDENCE = "needs_evidence"


class SecurityDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    decision: DecisionOutcome
    operation: str
    input_hash: str
    scope: dict[str, Any]
    evidence: list[dict[str, Any]]
    rationale: str
    restrictions: tuple[str, ...]
    risks: tuple[str, ...]
    version_constraint: str | None
    expires_at: datetime | None
    decided_at: datetime


def _evidence_entry(item: Mapping[str, Any] | UntrustedContent) -> dict[str, Any]:
    if isinstance(item, UntrustedContent):
        return item.as_evidence()
    return to_json(dict(item))


class ApprovalService:
    """Records WARDEN decisions. No other agent can create, alter, or delete them here."""

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
    ) -> SecurityDecision:
        if decided_by != AgentId.WARDEN:
            get_logger(EventCategory.SECURITY).warning(
                "security.decision_refused",
                extra={"decided_by": str(decided_by), "input_hash": operation.input_hash},
            )
            raise UnauthorizedDecisionMaker(
                f"only WARDEN may record security decisions, not {decided_by!s}"
            )
        outcome = DecisionOutcome(decision)
        if not rationale.strip():
            raise InvalidSecurityDecision("a security decision requires a rationale")
        # Unknown or malformed restrictions are rejected before anything is staged.
        # Imported here because permissions depends on this module.
        from aether.security.permissions import parse_restrictions

        parse_restrictions(list(restrictions))
        now = self._clock()
        expires_at = self._validate_expiry(outcome, expires_at, now)
        input_hash = operation.input_hash
        requirement = operation.security_fields()["declared_requirement_id"]
        stored_scope: dict[str, Any] = to_json(dict(scope or {}))
        stored_scope.update(
            {
                "input_hash": input_hash,
                "kind": operation.kind.value,
                "target": operation.target,
                "requested_by": operation.requested_by.value,
                "declared_requirement_id": requirement,
                "restrictions": list(restrictions),
                "risks": list(risks),
                "version_constraint": version_constraint,
                "expires_at": expires_at.isoformat() if expires_at else None,
                "decided_by": AgentId.WARDEN.value,
                "clearance_note": CLEARANCE_NOTE,
            }
        )
        evidence_entries = [_evidence_entry(item) for item in evidence]
        async with self._sessions() as session, session.begin():
            record = await SecurityDecisionRepository.stage(
                session,
                decision=outcome.value,
                operation=operation.describe(),
                scope=stored_scope,
                evidence=evidence_entries,
                rationale=rationale,
                input_hash=input_hash,
                expires_at=expires_at,
                created_at=now,
                task_id=operation.task_id,
            )
            event = stage_event(
                session,
                self._recorder,
                "security.decision_recorded",
                actor=AgentId.WARDEN.value,
                aggregate_type="security_decision",
                aggregate_id=record.id,
                payload={
                    "operation": operation.describe(),
                    "decision": outcome.value,
                    "rationale": rationale,
                    "scope": stored_scope,
                    "evidence": evidence_entries,
                },
                objective_id=operation.objective_id,
                task_id=operation.task_id,
                correlation_id=correlation_id,
                idempotency_key=f"security.decision_recorded:{record.id}",
            )
            decision_id = record.id
        level = logging.INFO if outcome is DecisionOutcome.APPROVED else logging.WARNING
        log_security(event, level, input_hash=input_hash)
        return SecurityDecision(
            decision_id=decision_id,
            decision=outcome,
            operation=operation.describe(),
            input_hash=input_hash,
            scope=stored_scope,
            evidence=evidence_entries,
            rationale=rationale,
            restrictions=tuple(restrictions),
            risks=tuple(risks),
            version_constraint=version_constraint,
            expires_at=expires_at,
            decided_at=now,
        )

    def _validate_expiry(
        self, outcome: DecisionOutcome, expires_at: datetime | None, now: datetime
    ) -> datetime | None:
        if expires_at is not None and expires_at.utcoffset() is None:
            raise InvalidSecurityDecision("expires_at must include a timezone")
        if outcome is not DecisionOutcome.APPROVED:
            return expires_at.astimezone(UTC) if expires_at else None
        latest = now + self._policy.max_approval_ttl
        if expires_at is None:
            return latest
        if expires_at <= now:
            raise InvalidSecurityDecision("an approval must expire in the future")
        if expires_at > latest:
            raise InvalidSecurityDecision("approval lifetime exceeds the policy maximum")
        return expires_at.astimezone(UTC)
