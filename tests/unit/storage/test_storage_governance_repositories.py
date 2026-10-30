from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from aether.storage.models import (
    AgentHealthStatus,
    AgentHeartbeatRecord,
    AuthorizationRequestRecord,
    AuthorizationStatus,
    CapabilityStatus,
    FindingClassification,
    FindingStatus,
    ResearchFindingRecord,
)
from aether.storage.repositories import (
    AgentHeartbeatRepository,
    AgentRepository,
    AuthorizationRequestRepository,
    CapabilityRepository,
    ErrorRepository,
    EvidenceInput,
    InvalidAuthorizationResponse,
    InvalidCapabilityTransition,
    InvalidFindingTransition,
    ResearchFindingRepository,
    agent_aggregate_id,
    allowed_capability_transitions,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


async def test_capability_must_be_tested_before_verification(database, events_for):
    repository = CapabilityRepository(database.sessions)
    capability_id = await repository.register(name="pdf-ocr", actor="SCOUT", provider="local")

    with pytest.raises(InvalidCapabilityTransition):
        await repository.transition(capability_id, CapabilityStatus.VERIFIED, actor="LEDGER")
    approval_path = (
        CapabilityStatus.PROPOSED,
        CapabilityStatus.APPROVED,
        CapabilityStatus.INSTALLED,
    )
    for status in approval_path:
        await repository.transition(capability_id, status, actor="ORCHESTR")
    with pytest.raises(InvalidCapabilityTransition):
        await repository.transition(capability_id, CapabilityStatus.VERIFIED, actor="LEDGER")
    await repository.transition(capability_id, CapabilityStatus.TESTED, actor="LEDGER")
    await repository.transition(capability_id, CapabilityStatus.VERIFIED, actor="LEDGER")

    capability = await repository.get_by_name("pdf-ocr")
    assert capability.lifecycle_status == CapabilityStatus.VERIFIED
    assert capability.verification_status == "verified"
    verified = await repository.list_by_status(CapabilityStatus.VERIFIED)
    assert [record.id for record in verified] == [capability_id]

    events = await events_for(capability_id)
    assert sorted(event.event_type for event in events) == [
        "capability.discovered",
        *["capability.transitioned"] * 5,
    ]
    assert {event.category for event in events} == {"capability"}


async def test_capability_names_are_unique(database):
    repository = CapabilityRepository(database.sessions)
    await repository.register(name="pdf-ocr", actor="SCOUT")

    with pytest.raises(IntegrityError):
        await repository.register(name="pdf-ocr", actor="ROGUE")


def test_only_tested_capabilities_can_become_verified():
    for status in CapabilityStatus:
        allowed = allowed_capability_transitions(status)
        assert (CapabilityStatus.VERIFIED in allowed) is (status is CapabilityStatus.TESTED)
    assert allowed_capability_transitions(CapabilityStatus.DEPRECATED) == frozenset()


async def test_research_finding_lifecycle(database, events_for, objective_id):
    repository = ResearchFindingRepository(database.sessions)
    with pytest.raises(ValueError):
        await repository.submit(
            agent="SCOUT",
            question="Which OCR engine?",
            findings="Unsupported claim",
            confidence=0.9,
            classification=FindingClassification.VERIFIED,
        )
    with pytest.raises(ValueError):
        await repository.submit(
            agent="SCOUT",
            question="Which OCR engine?",
            findings="Overconfident",
            confidence=1.5,
            classification=FindingClassification.HYPOTHESIS,
        )

    finding_id = await repository.submit(
        agent="ROGUE",
        question="Which OCR engine?",
        findings="Tesseract handles the sample documents",
        confidence=0.8,
        classification=FindingClassification.VERIFIED,
        objective_id=objective_id,
        evidence=[EvidenceInput(source_uri="https://example.invalid/ocr", summary="docs")],
    )
    await repository.add_evidence(
        finding_id,
        EvidenceInput(source_uri="file:///benchmarks/ocr.txt", summary="benchmark"),
        actor="ROGUE",
    )
    with pytest.raises(ValueError):
        await repository.set_status(finding_id, FindingStatus.REJECTED, actor="SCOUT")
    await repository.set_status(finding_id, FindingStatus.CONSOLIDATED, actor="SCOUT")
    await repository.set_status(
        finding_id, FindingStatus.ADOPTED, actor="SCOUT", rationale="best fit"
    )
    with pytest.raises(InvalidFindingTransition):
        await repository.set_status(
            finding_id, FindingStatus.REJECTED, actor="SCOUT", rationale="too late"
        )

    evidence = await repository.list_evidence(finding_id)
    assert len(evidence) == 2
    assert {item.trust_level for item in evidence} == {"untrusted"}
    async with database.sessions() as session:
        finding = await session.get(ResearchFindingRecord, finding_id)
    assert finding.status == FindingStatus.ADOPTED
    assert finding.consolidated_by == "SCOUT"
    assert finding.rationale == "best fit"

    events = await events_for(finding_id)
    assert sorted(event.event_type for event in events) == [
        "finding.evidence_added",
        "finding.status_changed",
        "finding.status_changed",
        "finding.submitted",
    ]
    assert {event.objective_id for event in events} == {objective_id}


async def test_authorization_requires_explicit_unexpired_approval(database, events_for):
    repository = AuthorizationRequestRepository(database.sessions)
    request_id = await repository.create(
        question="Install pdf-ocr?",
        actor="MUSE",
        expires_at=NOW + timedelta(hours=1),
    )

    assert not await repository.is_authorized(request_id, now=NOW)
    status = await repository.respond(
        request_id, approved=True, actor="USER", response="yes", now=NOW
    )
    assert status is AuthorizationStatus.APPROVED
    assert await repository.is_authorized(request_id, now=NOW)
    assert not await repository.is_authorized(request_id, now=NOW + timedelta(hours=2))
    with pytest.raises(InvalidAuthorizationResponse):
        await repository.respond(request_id, approved=False, actor="USER", now=NOW)

    events = await events_for(request_id)
    assert sorted(event.event_type for event in events) == [
        "authorization.approved",
        "authorization.requested",
    ]
    assert {event.category for event in events} == {"authorization"}


async def test_late_authorization_response_expires_request(database, events_for):
    repository = AuthorizationRequestRepository(database.sessions)
    request_id = await repository.create(question="Delete cache?", actor="MUSE", expires_at=NOW)

    status = await repository.respond(
        request_id, approved=True, actor="USER", now=NOW + timedelta(minutes=1)
    )

    assert status is AuthorizationStatus.EXPIRED
    assert not await repository.is_authorized(request_id, now=NOW - timedelta(minutes=1))
    async with database.sessions() as session:
        request = await session.get(AuthorizationRequestRecord, request_id)
    assert request.status == AuthorizationStatus.EXPIRED
    assert request.responded_at is None
    assert sorted(event.event_type for event in await events_for(request_id)) == [
        "authorization.expired",
        "authorization.requested",
    ]


async def test_expiring_overdue_requests_never_approves_them(database):
    repository = AuthorizationRequestRepository(database.sessions)
    overdue = await repository.create(
        question="Overdue?",
        actor="MUSE",
        expires_at=NOW - timedelta(minutes=1),
    )
    open_ended = await repository.create(question="No deadline?", actor="MUSE")
    later = await repository.create(
        question="Later?",
        actor="MUSE",
        expires_at=NOW + timedelta(hours=1),
    )

    assert await repository.expire_overdue(now=NOW) == [overdue]

    async with database.sessions() as session:
        statuses = {
            request_id: (await session.get(AuthorizationRequestRecord, request_id)).status
            for request_id in (overdue, open_ended, later)
        }
    assert statuses == {
        overdue: AuthorizationStatus.EXPIRED,
        open_ended: AuthorizationStatus.PENDING,
        later: AuthorizationStatus.PENDING,
    }
    for request_id in (overdue, open_ended, later):
        assert not await repository.is_authorized(request_id, now=NOW)


async def test_errors_and_recovery_attempts_are_audited(database, events_for):
    repository = ErrorRepository(database.sessions)
    error_id = await repository.record(
        error_type="ProviderTimeout",
        message="timed out",
        agent="SCOUT",
    )
    await repository.record_recovery_attempt(
        error_id, action="retry", outcome="failed", actor="ORCHESTR"
    )
    assert [error.id for error in await repository.list_unresolved()] == [error_id]

    await repository.record_recovery_attempt(
        error_id,
        action="switch provider",
        outcome="succeeded",
        actor="ORCHESTR",
        resolves=True,
    )

    assert await repository.list_unresolved() == []
    attempts = await repository.list_recovery_attempts(error_id)
    assert sorted(attempt.action for attempt in attempts) == ["retry", "switch provider"]
    events = await events_for(error_id)
    assert sorted(event.event_type for event in events) == [
        "error.recorded",
        "error.recovery_attempted",
        "error.recovery_attempted",
    ]
    assert {event.category for event in events} == {"error"}


async def test_agent_configuration_changes_are_audited(database, events_for):
    repository = AgentRepository(database.sessions)

    assert await repository.upsert("SCOUT", responsibility="research")
    assert not await repository.upsert(
        "SCOUT", responsibility="research and discovery", model_preference="local"
    )
    await repository.set_enabled("SCOUT", False, actor="ORCHESTR")
    with pytest.raises(LookupError):
        await repository.set_enabled("UNKNOWN", True, actor="ORCHESTR")

    agent = await repository.get("SCOUT")
    assert agent.enabled is False
    assert agent.responsibility == "research and discovery"
    assert agent.model_preference == "local"
    events = await events_for(agent_aggregate_id("SCOUT"))
    assert sorted(event.event_type for event in events) == [
        "agent.disabled",
        "agent.registered",
        "agent.updated",
    ]


async def test_every_heartbeat_is_audited_and_status_changes_are_distinct(database, events_for):
    await AgentRepository(database.sessions).upsert("LEDGER", responsibility="verification")
    heartbeats = AgentHeartbeatRepository(database.sessions)
    statuses = (
        AgentHealthStatus.IDLE,
        AgentHealthStatus.IDLE,
        AgentHealthStatus.BUSY,
        AgentHealthStatus.BUSY,
        AgentHealthStatus.IDLE,
    )
    for status in statuses:
        await heartbeats.record("LEDGER", status)

    latest = await heartbeats.latest("LEDGER")
    assert latest.status == AgentHealthStatus.IDLE
    async with database.sessions() as session:
        count = await session.scalar(select(func.count()).select_from(AgentHeartbeatRecord))
    assert count == len(statuses)
    events = await events_for(agent_aggregate_id("LEDGER"))
    heartbeat_events = [event for event in events if event.event_type == "agent.heartbeat"]
    changes = [event for event in events if event.event_type == "agent.status_changed"]
    assert len(heartbeat_events) == 2
    assert sorted((event.payload["from"] or "", event.payload["to"]) for event in changes) == [
        ("", "idle"),
        ("busy", "idle"),
        ("idle", "busy"),
    ]
