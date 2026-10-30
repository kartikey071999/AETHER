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
