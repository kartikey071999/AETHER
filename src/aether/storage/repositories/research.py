"""Research findings, their supporting evidence, and SCOUT consolidation decisions."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    EventCategory,
    FindingClassification,
    FindingEvidenceRecord,
    FindingStatus,
    ResearchFindingRecord,
    utcnow,
)
from aether.storage.repositories._events import add_event

_ALLOWED_FINDING_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.SUBMITTED: frozenset(
        {
            FindingStatus.CONSOLIDATED,
            FindingStatus.ADOPTED,
            FindingStatus.REJECTED,
            FindingStatus.NEEDS_EVIDENCE,
        }
    ),
    FindingStatus.NEEDS_EVIDENCE: frozenset({FindingStatus.SUBMITTED, FindingStatus.REJECTED}),
    FindingStatus.CONSOLIDATED: frozenset(
        {FindingStatus.ADOPTED, FindingStatus.REJECTED, FindingStatus.NEEDS_EVIDENCE}
    ),
    FindingStatus.ADOPTED: frozenset(),
    FindingStatus.REJECTED: frozenset(),
}


class InvalidFindingTransition(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceInput:
    source_uri: str
    summary: str = ""
    content_hash: str | None = None
    trust_level: str = "untrusted"
    retrieved_at: datetime | None = None


class ResearchFindingRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def submit(
        self,
        *,
        agent: str,
        question: str,
        findings: str,
        confidence: float,
        classification: FindingClassification,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        candidate_approaches: list[dict[str, Any]] | None = None,
        recommendation: str | None = None,
        rationale: str | None = None,
        evidence: Sequence[EvidenceInput] = (),
        correlation_id: UUID | None = None,
    ) -> UUID:
        classification = FindingClassification(classification)
        if not 0 <= confidence <= 1:
            raise ValueError("Finding confidence must be between 0 and 1")
        if classification is FindingClassification.VERIFIED and not evidence:
            raise ValueError("A verified finding must include supporting evidence")
        async with self._sessions() as session, session.begin():
            finding = ResearchFindingRecord(
                objective_id=objective_id,
                task_id=task_id,
                agent=agent,
                question=question,
                findings=findings,
                candidate_approaches=candidate_approaches or [],
                recommendation=recommendation,
                confidence=confidence,
                classification=classification,
                status=FindingStatus.SUBMITTED,
                rationale=rationale,
            )
            session.add(finding)
            await session.flush()
            for item in evidence:
                session.add(_evidence_record(finding.id, item))
            add_event(
                session,
                event_type="finding.submitted",
                aggregate_type="research_finding",
                aggregate_id=finding.id,
                actor=agent,
                payload={
                    "question": question,
                    "classification": classification,
                    "confidence": confidence,
                    "evidence_count": len(evidence),
                },
                category=EventCategory.RESEARCH,
                correlation_id=correlation_id,
                objective_id=objective_id,
                task_id=task_id,
                idempotency_key=f"finding.submitted:{finding.id}",
            )
            return finding.id

    async def add_evidence(
        self,
        finding_id: UUID,
        evidence: EvidenceInput,
        *,
        actor: str,
        correlation_id: UUID | None = None,
    ) -> UUID:
        async with self._sessions() as session, session.begin():
            finding = await _lock_finding(session, finding_id)
            record = _evidence_record(finding.id, evidence)
            session.add(record)
            finding.updated_at = utcnow()
            await session.flush()
            add_event(
                session,
                event_type="finding.evidence_added",
                aggregate_type="research_finding",
                aggregate_id=finding.id,
                actor=actor,
                payload={
                    "evidence_id": record.id,
                    "source_uri": evidence.source_uri,
                    "trust_level": evidence.trust_level,
                },
                category=EventCategory.RESEARCH,
                correlation_id=correlation_id,
                objective_id=finding.objective_id,
                task_id=finding.task_id,
            )
            return record.id

    async def set_status(
        self,
        finding_id: UUID,
        new_status: FindingStatus,
        *,
        actor: str,
        rationale: str | None = None,
        correlation_id: UUID | None = None,
    ) -> None:
        new_status = FindingStatus(new_status)
        if new_status is FindingStatus.REJECTED and not rationale:
            raise ValueError("Rejecting a finding requires a recorded rationale")
        async with self._sessions() as session, session.begin():
            finding = await _lock_finding(session, finding_id)
            old_status = FindingStatus(finding.status)
            if new_status not in _ALLOWED_FINDING_TRANSITIONS[old_status]:
                raise InvalidFindingTransition(
                    f"Cannot transition finding from {old_status} to {new_status}"
                )
            finding.status = new_status
            finding.updated_at = utcnow()
            if rationale is not None:
                finding.rationale = rationale
            if new_status is FindingStatus.CONSOLIDATED:
                finding.consolidated_by = actor
            add_event(
                session,
                event_type="finding.status_changed",
                aggregate_type="research_finding",
                aggregate_id=finding.id,
                actor=actor,
                payload={
                    "from_status": old_status,
                    "to_status": new_status,
                    "rationale": rationale,
                },
                category=EventCategory.RESEARCH,
                correlation_id=correlation_id,
                objective_id=finding.objective_id,
                task_id=finding.task_id,
            )

    async def list_evidence(self, finding_id: UUID) -> list[FindingEvidenceRecord]:
        condition = FindingEvidenceRecord.finding_id == finding_id
        statement = select(FindingEvidenceRecord).where(condition)
        async with self._sessions() as session:
            records = await session.scalars(statement.order_by(FindingEvidenceRecord.retrieved_at))
            return list(records)


def _evidence_record(finding_id: UUID, evidence: EvidenceInput) -> FindingEvidenceRecord:
    return FindingEvidenceRecord(
        finding_id=finding_id,
        source_uri=evidence.source_uri,
        summary=evidence.summary,
        content_hash=evidence.content_hash,
        trust_level=evidence.trust_level,
        retrieved_at=evidence.retrieved_at or utcnow(),
    )


async def _lock_finding(session: AsyncSession, finding_id: UUID) -> ResearchFindingRecord:
    statement = select(ResearchFindingRecord).where(ResearchFindingRecord.id == finding_id)
    finding = await session.scalar(statement.with_for_update())
    if finding is None:
        raise LookupError(f"Research finding {finding_id} does not exist")
    return finding
