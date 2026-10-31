from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aether.observability.events import (
    EVENT_CATEGORIES,
    EVENT_TYPES,
    AetherEvent,
    EventCategory,
    ObjectiveCreatedPayload,
    build_event,
)


def test_event_envelope_and_payload_are_frozen_and_typed(event: AetherEvent) -> None:
    assert isinstance(event.event_id, UUID)
    assert isinstance(event.correlation_id, UUID)
    assert event.timestamp.tzinfo is UTC
    assert event.category is EventCategory.ERROR
    with pytest.raises(ValidationError):
        event.actor = "other"
    with pytest.raises(ValidationError):
        event.payload.message = "changed"
    assert event.model_dump(mode="json")["payload"]["error_type"] == "ExampleError"
    assert AetherEvent.model_validate_json(event.model_dump_json()) == event


def test_build_validates_dictionary_and_model_payloads() -> None:
    kwargs = {
        "source": "test",
        "actor": "ORCHESTR",
        "aggregate_type": "objective",
        "aggregate_id": uuid4(),
    }
    event = build_event("objective.created", payload={"title": "Observe"}, **kwargs)
    assert isinstance(event.payload, ObjectiveCreatedPayload)
    assert event.payload.title == "Observe"
    rebuilt = build_event("objective.created", payload=event.payload, **kwargs)
    assert rebuilt.payload == event.payload
    with pytest.raises(ValidationError):
        build_event("objective.created", payload={"priority": 101}, **kwargs)
    with pytest.raises(ValidationError):
        build_event("objective.created", payload={"title": "Observe", "unknown": True}, **kwargs)
    with pytest.raises(ValidationError):
        build_event("objective.created", payload=event.payload, extra_field=True, **kwargs)
    with pytest.raises(ValidationError):
        build_event("objective.created", payload=event.payload, category="git", **kwargs)


def test_unknown_types_rejected_in_factory_and_direct_construction(event: AetherEvent) -> None:
    with pytest.raises(ValueError, match="unknown event type"):
        build_event("made.up", payload={})
    document = event.model_dump()
    document["event_type"] = "made.up"
    with pytest.raises(ValidationError, match="unknown event type"):
        AetherEvent.model_validate(document)
    document["event_type"] = "objective.created"
    with pytest.raises(ValidationError):
        AetherEvent.model_validate(document)


def test_timestamp_rejects_naive_and_normalizes_offsets(event: AetherEvent) -> None:
    document = event.model_dump()
    document["timestamp"] = datetime(2026, 1, 1)
    with pytest.raises(ValidationError, match="timezone"):
        AetherEvent.model_validate(document)
    document["timestamp"] = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5)))
    normalized = AetherEvent.model_validate(document)
    assert normalized.timestamp == datetime(2025, 12, 31, 19, tzinfo=UTC)
    assert normalized.timestamp.tzinfo is UTC


PAYLOAD_EXAMPLES = {
    "objective.created": {"title": "Observe"},
    "objective.updated": {"changes": {"priority": 80}, "rationale": "Important"},
    "objective.base_priority_changed": {
        "from_priority": 50,
        "to_priority": 80,
        "reason": "new baseline",
    },
    "task.priority_changed": {"from_priority": 50, "to_priority": 80, "reason": "urgent"},
    "task.estimates_changed": {"duration_s": 10, "ram_mb": 256, "disk_mb": 512, "cpu_percent": 20},
    "test.run_created": {"suite": "unit"},
    "test.results_added": {
        "results": [{"result_id": str(uuid4()), "name": "offline", "outcome": "passed"}]
    },
    "task.created": {"title": "Record"},
    "task.transitioned": {"from_status": "pending", "to_status": "ready", "reason": "Ready"},
    "task.assigned": {"assigned_agent": "LEDGER", "reason": "Record keeping"},
    "message.published": {
        "message_id": str(uuid4()),
        "source_agent": "ORCHESTR",
        "target_agent": "LEDGER",
        "message_type": "task.assign",
    },
    "message.delivered": {"message_id": str(uuid4()), "target_agent": "LEDGER"},
    "message.processed": {
        "message_id": str(uuid4()),
        "target_agent": "LEDGER",
        "outcome": "Done",
    },
    "message.failed": {
        "message_id": str(uuid4()),
        "target_agent": "LEDGER",
        "error": "Unavailable",
    },
    "security.decision_recorded": {
        "operation": "build",
        "decision": "allowed",
        "rationale": "Approved",
    },
    "security.operation_blocked": {"operation": "install", "reason": "Not authorized"},
    "execution.started": {"execution_id": str(uuid4()), "operation": "build"},
    "execution.finished": {
        "execution_id": str(uuid4()),
        "success": True,
        "duration_seconds": 2.0,
    },
    "test.run_finished": {
        "suite": "unit",
        "passed": 1,
        "failed": 0,
        "duration_seconds": 0.1,
    },
    "capability.status_changed": {
        "capability_id": str(uuid4()),
        "previous_status": "experimental",
        "status": "available",
        "reason": "Verified",
    },
    "resource.measured": {"cpu_percent": 12, "available_ram_bytes": 123, "free_disk_bytes": 456},
    "scheduler.decision": {"reason": "No ready tasks"},
    "agent.heartbeat": {"agent_id": "LEDGER", "status": "idle"},
    "error.raised": {"error_type": "ExampleError", "message": "Failure"},
    "recovery.attempted": {
        "operation": "replay",
        "attempt": 1,
        "success": True,
        "reason": "Retry",
    },
    "decision.recorded": {"decision": "defer", "rationale": "Resource limit"},
    "copilot.session_started": {"model": "mock", "prompt_summary": "Task"},
    "copilot.session_finished": {"outcome": "done", "duration_seconds": 1},
    "copilot.report_received": {"report": {"summary": "Done"}, "ready_for_integration": True},
    "git.branch_created": {"branch": "feature/observability", "base_branch": "develop"},
    "git.commit_created": {"commit_hash": "abc123", "message": "Add logging"},
    "git.pr_opened": {
        "number": 1,
        "url": "https://example.invalid/pr/1",
        "head_branch": "feature/observability",
        "base_branch": "develop",
    },
    "git.merge_completed": {
        "source_branch": "feature/observability",
        "target_branch": "develop",
        "commit_hash": "abc123",
    },
    "provider.request_completed": {
        "provider": "mock",
        "model": "mock-model",
        "tokens": 120,
        "cost": 0.001,
        "latency_ms": 20,
    },
    "objective.priority_changed": {"from_priority": 10, "to_priority": 20, "reason": "Urgent"},
    "objective.status_changed": {"from_status": "active", "to_status": "paused"},
    "task.requirement_linked": {"requirement_id": str(uuid4()), "reason": "Approved"},
    "task.dependency_added": {"depends_on_task_id": str(uuid4())},
    "task.dependency_removed": {"depends_on_task_id": str(uuid4())},
    "task.lease_acquired": {
        "owner": "engine-1",
        "attempt": 1,
        "expires_at": "2026-01-01T00:00:00+00:00",
    },
    "task.lease_released": {"owner": "engine-1", "final_status": "ready"},
    "task.lease_expired": {"owner": "engine-1"},
    "task.completed": {"result": {"ok": True}},
    "task.blocked": {"reason": "Waiting"},
    "task.waiting_for_approval": {"reason": "Needs approval"},
    "task.failed": {"error": "boom", "will_retry": False},
    "task.timed_out": {"error": "timeout", "attempt": 1, "max_attempts": 3},
    "task.cancelled": {"reason": "Cancelled"},
    "task.interrupted": {"reason": "shutdown"},
    "task.control_requested": {
        "task_id": "00000000-0000-4000-8000-000000000001",
        "action": "pause",
        "requested_by": "ORCHESTR",
        "reason": "Resource pressure",
    },
    "task.control_resolved": {
        "task_id": "00000000-0000-4000-8000-000000000001",
        "action": "pause",
        "requested_by": "ORCHESTR",
        "reason": "Resource pressure",
        "outcome": "paused",
        "request_event_id": "00000000-0000-4000-8000-000000000002",
    },
    "message.processing": {"message_id": str(uuid4()), "target_agent": "LEDGER"},
    "message.requeued": {
        "message_id": str(uuid4()),
        "target_agent": "LEDGER",
        "previous_status": "processing",
    },
    "capability.discovered": {"name": "pytest"},
    "capability.transitioned": {"from_status": "discovered", "to_status": "researching"},
    "requirement.created": {"title": "Need", "requirement_type": "capability"},
    "requirement.status_changed": {"from_status": "draft", "to_status": "approved"},
    "finding.submitted": {
        "question": "What?",
        "classification": "fact",
        "confidence": 0.5,
        "evidence_count": 1,
    },
    "finding.evidence_added": {"evidence_id": str(uuid4())},
    "finding.status_changed": {"from_status": "open", "to_status": "accepted"},
    "authorization.requested": {"question": "Install it?"},
    "authorization.approved": {"response": "yes"},
    "authorization.denied": {"response": "no"},
    "authorization.expired": {},
    "person.created": {"access_level": "owner"},
    "person.deleted": {},
    "preference.set": {
        "person_id": str(uuid4()),
        "name": "tone",
        "value": "brief",
        "source": "user",
    },
    "interaction.recorded": {"channel": "cli", "direction": "inbound"},
    "kg.entity_created": {"entity_type": "tool", "name": "ruff"},
    "kg.entity_updated": {"entity_type": "tool", "name": "ruff"},
    "kg.relation_created": {
        "source_entity_id": str(uuid4()),
        "target_entity_id": str(uuid4()),
        "relation_type": "uses",
    },
    "provider.usage_recorded": {
        "provider": "mock",
        "model": "mock-model",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": "0.0012",
        "status": "ok",
    },
    "runtime.started": {"owner": "engine-1", "spool_replayed": 0, "spool_failed": 0},
    "runtime.stopping": {"running_tasks": 0},
    "runtime.stopped": {"released_leases": 0},
    "runtime.security_initialized": {"policy_fingerprint": "a" * 64, "tool_count": 4},
    "runtime.models_initialized": {
        "mock": True,
        "providers": [{"name": "mock", "available": True, "model_count": 1}],
        "model_count": 1,
        "max_concurrent_llm_requests": 2,
    },
    "provider.budget_exhausted": {
        "agent": "FORGE",
        "reason": "objective token budget",
        "requests": 3,
        "tokens": 20_500,
        "max_requests": 10,
        "max_tokens": 20_000,
    },
    "provider.circuit_opened": {
        "agent": "FORGE",
        "consecutive_failures": 5,
        "threshold": 5,
        "last_error": "NoModelAvailable",
    },
    "provider.circuit_closed": {"agent": "FORGE", "probe": True, "consecutive_failures": 5},
    "agent.registered": {"agent_id": "LEDGER", "enabled": True},
    "agent.updated": {"agent_id": "LEDGER", "enabled": False},
    "agent.enabled": {"agent_id": "LEDGER"},
    "agent.disabled": {"agent_id": "LEDGER"},
    "agent.status_changed": {"agent_id": "LEDGER", "from_status": "idle", "to_status": "busy"},
    "error.recorded": {"error_type": "ExampleError", "message": "Failure"},
    "error.recovery_attempted": {
        "recovery_attempt_id": str(uuid4()),
        "action": "retry",
        "outcome": "ok",
        "resolved": True,
    },
    "error.resolved": {"note": "fixed"},
    # --- security / tools events (T-020) ---
    "security.operation_authorized": {
        "operation": "subprocess:git",
        "kind": "subprocess",
        "input_hash": "0" * 64,
        "grant_id": str(uuid4()),
        "expires_at": "2026-01-01T00:00:00+00:00",
    },
    "security.user_authorization_requested": {
        "operation": "config_change:settings",
        "input_hash": "0" * 64,
        "request_id": str(uuid4()),
        "decision_id": str(uuid4()),
        "question": "Allow the change?",
    },
}


def test_catalogue_covers_all_required_types_and_categories() -> None:
    assert set(EVENT_TYPES) == set(PAYLOAD_EXAMPLES)
    assert set(EVENT_CATEGORIES) == set(EVENT_TYPES)
    assert {item.value for item in EventCategory} == {
        "runtime",
        "agent",
        "copilot",
        "git",
        "security",
        "test",
        "resource",
        "error",
        "decision",
        "objective",
        "task",
        "message",
        "capability",
        "requirement",
        "research",
        "authorization",
        "personalization",
        "knowledge",
        "provider",
    }


@pytest.mark.parametrize("event_type,payload", PAYLOAD_EXAMPLES.items())
def test_every_catalogue_entry_roundtrips(event_type: str, payload: dict) -> None:
    event = build_event(
        event_type,
        source="unit-test",
        actor="system",
        aggregate_type="test",
        aggregate_id=uuid4(),
        payload=payload,
    )
    assert isinstance(event.payload, EVENT_TYPES[event_type])
    assert AetherEvent.model_validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize(
    "field,value", [("tokens", -1), ("cost", -1), ("latency_ms", float("nan"))]
)
def test_provider_measurements_reject_invalid_values(field: str, value: float) -> None:
    payload = PAYLOAD_EXAMPLES["provider.request_completed"] | {field: value}
    with pytest.raises(ValidationError):
        EVENT_TYPES["provider.request_completed"].model_validate(payload)
