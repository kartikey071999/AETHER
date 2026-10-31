from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from aether.agents.base import AgentContext, TaskResultStatus
from aether.agents.registry import AGENT_DEFINITIONS
from aether.agents.security_review import request_security_review
from aether.communication import contracts
from aether.communication.contracts import (
    MESSAGE_TYPE_BY_MODEL,
    PAYLOAD_MODELS,
    ROUTES,
    SENDERS,
    BlockerPayload,
    InvalidPayload,
    ObjectiveCreatePayload,
    ResearchFindingPayload,
    RouteViolation,
    TaskOutcome,
    UnknownMessageType,
    UserResponsePayload,
    authorization_granted,
    build_message,
    check_route,
    receivable_types,
    sendable_types,
    validate_message,
    validate_payload,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.operations import Operation, OperationKind

HASH = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
OBJECTIVE = UUID("00000000-0000-4000-8000-000000000001")
TASK = UUID("00000000-0000-4000-8000-000000000002")
REQUIREMENT = UUID("00000000-0000-4000-8000-000000000003")
FINDING = UUID("00000000-0000-4000-8000-000000000004")
AUTH_REQUEST = UUID("00000000-0000-4000-8000-000000000005")

ESTIMATE = {"ram_mb": 256, "disk_mb": 100, "cpu_percent": 25.0, "duration_s": 60}
STEP = {"tool": "subprocess", "arguments": {"argv": ["pytest", "-q"]}, "purpose": "run tests"}


def operation(requested_by: AgentId = AgentId.FORGE) -> Operation:
    return Operation(
        kind=OperationKind.PACKAGE_INSTALL,
        target="pip install webrtcvad==2.0.10",
        arguments={"package": "webrtcvad", "version": "2.0.10"},
        requested_by=requested_by,
        task_id=TASK,
    )


# One realistic (source, target, payload) sample per message type.
SAMPLES: dict[MessageType, tuple[AgentId, AgentId, dict]] = {
    MessageType.OBJECTIVE_CREATE: (
        AgentId.SCOUT,
        AgentId.ORCHESTR,
        {
            "title": "Detect user presence for voice interaction",
            "description": "Determine whether the user is available.",
            "priority": 60,
            "completion_criteria": ["presence detected locally with >90% accuracy"],
            "rationale": "Enables voice interaction",
            "expected_benefit": "Fewer interruptions",
            "estimated_cost": 2.5,
            "stopping_conditions": ["no local solution after 3 investigations"],
            "deadline": LATER.isoformat(),
            "generated": True,
            "parent_objective_id": None,
        },
    ),
    MessageType.OBJECTIVE_UPDATE: (
        AgentId.ORCHESTR,
        AgentId.LEDGER,
        {"objective_id": str(OBJECTIVE), "reason": "new deadline", "priority": 80},
    ),
    MessageType.OBJECTIVE_COMPLETED: (
        AgentId.ORCHESTR,
        AgentId.LEDGER,
        {
            "objective_id": str(OBJECTIVE),
            "summary": "presence detection verified",
            "criteria_results": [
                {"criterion": "works offline", "passed": True, "evidence": "test run 7"}
            ],
        },
    ),
    MessageType.TASK_CREATE: (
        AgentId.SCOUT,
        AgentId.ORCHESTR,
        {
            "objective_id": str(OBJECTIVE),
            "title": "Evaluate two VAD libraries",
            "suggested_agent": "SCOUT",
            "acceptance_criteria": ["comparison recorded"],
            "depends_on": [str(TASK)],
            "resource_estimate": ESTIMATE,
        },
    ),
    MessageType.TASK_ASSIGN: (
        AgentId.ORCHESTR,
        AgentId.FORGE,
        {
            "task_id": str(TASK),
            "objective_id": str(OBJECTIVE),
            "title": "Install webrtcvad",
            "description": "Install the approved VAD library",
            "assigned_agent": "FORGE",
            "acceptance_criteria": ["import succeeds"],
            "requirement_ids": [str(REQUIREMENT)],
            "depends_on": [],
            "priority": 70,
            "resource_estimate": ESTIMATE,
            "interruptible": False,
        },
    ),
    MessageType.TASK_TRANSITION: (
        AgentId.FORGE,
        AgentId.LEDGER,
        {"task_id": str(TASK), "from_state": "running", "to_state": "blocked", "reason": "x"},
    ),
    MessageType.TASK_RESULT: (
        AgentId.FORGE,
        AgentId.ORCHESTR,
        {"task_id": str(TASK), "status": "completed", "summary": "done", "outputs": {"n": 1}},
    ),
    MessageType.RESEARCH_REQUEST: (
        AgentId.ORCHESTR,
        AgentId.ROGUE,
        {
            "objective_id": str(OBJECTIVE),
            "question": "Which unconventional presence signals exist?",
            "scope": "local only",
            "constraints": ["no cloud audio"],
            "budget": {"max_llm_requests": 20, "max_sources": 10},
            "review_by": LATER.isoformat(),
            "mode": "unconventional",
        },
    ),
    MessageType.RESEARCH_FINDING: (
        AgentId.ROGUE,
        AgentId.SCOUT,
        {
            "finding_id": str(FINDING),
            "objective_id": str(OBJECTIVE),
            "question": "Which presence signals exist?",
            "summary": "Bluetooth RSSI from the user's phone is a usable signal",
            "candidate_approaches": [
                {
                    "name": "BLE RSSI",
                    "description": "Scan for the paired phone",
                    "pros": ["cheap"],
                    "cons": ["phone may be elsewhere"],
                    "dependencies": ["bleak"],
                    "resource_needs": {"ram_mb": 50},
                    "risks": ["requires bluetooth permission"],
                }
            ],
            "recommendation": "prototype BLE RSSI",
            "confidence": 0.6,
            "classification": "verified",
            "evidence": [
                {
                    "source_uri": "https://example.invalid/ble",
                    "summary": "benchmark",
                    "content_hash": HASH,
                    "trust": "untrusted",
                }
            ],
            "security_considerations": ["only paired devices"],
            "next_steps": ["prototype"],
            "submitted_by": "ROGUE",
        },
    ),
    MessageType.RESEARCH_CONSOLIDATED: (
        AgentId.SCOUT,
        AgentId.ORCHESTR,
        {
            "objective_id": str(OBJECTIVE),
            "decisions": [
                {"finding_id": str(FINDING), "decision": "adopt", "rationale": "best fit"},
                {"finding_id": str(uuid4()), "decision": "delegate_rogue", "rationale": "dig"},
            ],
            "requirement_ids": [str(REQUIREMENT)],
        },
    ),
    MessageType.REQUIREMENT_SUBMIT: (
        AgentId.ROGUE,
        AgentId.SCOUT,
        {
            "objective_id": str(OBJECTIVE),
            "title": "Audio must stay local",
            "requirement_type": "security",
            "acceptance_criteria": ["no outbound audio traffic"],
            "source_finding_id": str(FINDING),
            "security_review_required": True,
        },
    ),
    MessageType.REQUIREMENT_EVALUATED: (
        AgentId.SCOUT,
        AgentId.ORCHESTR,
        {
            "requirement_id": str(REQUIREMENT),
            "objective_id": str(OBJECTIVE),
            "title": "Audio must stay local",
            "requirement_type": "security",
            "acceptance_criteria": ["no outbound audio traffic"],
            "security_review_required": True,
            "outcome": "approved",
            "rationale": "aligned with privacy policy",
        },
    ),
    MessageType.SECURITY_EVALUATE: (
        AgentId.FORGE,
        AgentId.WARDEN,
        {
            "operation": operation().model_dump(mode="json"),
            "input_hash": operation().input_hash,
            "requested_by": "FORGE",
            "task_id": str(TASK),
            "evidence": [{"type": "pypi_metadata", "sha256": HASH}],
        },
    ),
    MessageType.SECURITY_DECISION: (
        AgentId.WARDEN,
        AgentId.FORGE,
        {
            "decision_id": str(uuid4()),
            "decision": "approved",
            "input_hash": HASH,
            "restrictions": ["no_network"],
            "expires_at": LATER.isoformat(),
            "rationale": "pinned version with integrity hash",
        },
    ),
    MessageType.AUTHORIZATION_REQUEST: (
        AgentId.WARDEN,
        AgentId.ORCHESTR,
        {
            "authorization_request_id": str(AUTH_REQUEST),
            "question": "May AETHER use the microphone?",
            "operation_summary": "credential_access:microphone",
            "expires_at": LATER.isoformat(),
        },
    ),
    MessageType.USER_RESPONSE: (
        AgentId.MUSE,
        AgentId.ORCHESTR,
        {
            "authorization_request_id": str(AUTH_REQUEST),
            "text": "yes",
            "decision": "approved",
            "responded_at": NOW.isoformat(),
        },
    ),
    MessageType.RESOURCE_REQUEST: (
        AgentId.FORGE,
        AgentId.ORCHESTR,
        {"task_id": str(TASK), "resources": ESTIMATE, "download_mb": 300, "reason": "build"},
    ),
    MessageType.IMPLEMENTATION_PLAN: (
        AgentId.FORGE,
        AgentId.ORCHESTR,
        {
            "requirement_id": str(REQUIREMENT),
            "steps": [STEP],
            "rollback_steps": [STEP],
            "resource_estimate": ESTIMATE,
            "acceptance_criteria": ["tests pass"],
        },
    ),
    MessageType.EXECUTION_REQUEST: (
        AgentId.ORCHESTR,
        AgentId.FORGE,
        {
            "task_id": str(TASK),
            "requirement_id": str(REQUIREMENT),
            "plan_id": str(uuid4()),
            "steps": [STEP],
            "acceptance_criteria": ["tests pass"],
            "resource_estimate": ESTIMATE,
            "security_decision_ids": [str(uuid4())],
        },
    ),
    MessageType.EXECUTION_RESULT: (
        AgentId.FORGE,
        AgentId.LEDGER,
        {
            "task_id": str(TASK),
            "execution_run_ids": [str(uuid4())],
            "status": "succeeded",
            "outputs": {"exit_code": 0},
            "artifacts": ["data/artifacts/build.log"],
            "evidence": [{"exit_code": 0}],
        },
    ),
    MessageType.VERIFICATION_REQUEST: (
        AgentId.FORGE,
        AgentId.LEDGER,
        {
            "task_id": str(TASK),
            "requirement_id": str(REQUIREMENT),
            "suite": "tests/capabilities/vad",
            "acceptance_criteria": ["detects speech"],
        },
    ),
    MessageType.TEST_REPORT: (
        AgentId.LEDGER,
        AgentId.ORCHESTR,
        {
            "test_run_id": str(uuid4()),
            "requirement_id": str(REQUIREMENT),
            "suite": "tests/capabilities/vad",
            "results": {"passed": 4, "failed": 0, "errors": 0, "skipped": 1},
            "cases": [{"name": "test_detects_speech", "outcome": "passed", "duration_ms": 12}],
            "acceptance_results": [
                {"criterion": "detects speech", "passed": True, "evidence": "4/4"}
            ],
            "verdict": "verified",
        },
    ),
    MessageType.CAPABILITY_REGISTER: (
        AgentId.LEDGER,
        AgentId.ORCHESTR,
        {
            "name": "local-vad",
            "lifecycle_status": "verified",
            "provider": "webrtcvad",
            "version": "2.0.10",
            "entrypoint": "aether.capabilities.vad:detect",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "boolean"},
            "required_permissions": ["audio.read"],
            "verification_status": "verified",
            "verification_evidence": [{"test_run": "7"}],
            "requirement_id": str(REQUIREMENT),
        },
    ),
    MessageType.BLOCKER: (
        AgentId.FORGE,
        AgentId.ORCHESTR,
        {
            "task_id": str(TASK),
            "kind": "research",
            "description": "build fails on this platform",
            "route_to": "SCOUT",
            "attempts": 2,
        },
    ),
    MessageType.PROGRESS_UPDATE: (
        AgentId.FORGE,
        AgentId.ORCHESTR,
        {"task_id": str(TASK), "summary": "halfway", "percent_complete": 50, "state": "running"},
    ),
    MessageType.HEALTH_EVENT: (
        AgentId.LEDGER,
        AgentId.ORCHESTR,
        {
            "component": "resource_monitor",
            "status": "degraded",
            "resources": {"cpu_percent": 95.0, "disk_free_mb": 900},
            "observed_at": NOW.isoformat(),
        },
    ),
    MessageType.COMMUNICATION_REQUEST: (
        AgentId.ORCHESTR,
        AgentId.MUSE,
        {
            "what_happened": "Installed local VAD",
            "results": [{"capability": "local-vad"}],
            "verified": ["detects speech offline"],
            "uncertain": ["accuracy in noisy rooms"],
            "decisions_required": ["enable microphone?"],
            "authorization_required": True,
            "authorization_request_id": str(AUTH_REQUEST),
            "audience": str(uuid4()),
            "urgency": "high",
        },
    ),
    MessageType.COMMUNICATION_DELIVERED: (
        AgentId.MUSE,
        AgentId.ORCHESTR,
        {
            "interaction_id": str(uuid4()),
            "channel": "cli",
            "rendered_text_hash": HASH,
            "delivered_at": NOW.isoformat(),
        },
    ),
}


def make(message_type: MessageType, payload: dict | None = None, **envelope) -> AgentMessage:
    source, target, sample = SAMPLES[message_type]
    values = {"source_agent": source, "target_agent": target, "message_type": message_type}
    values.update(envelope)
    return AgentMessage(payload=sample if payload is None else payload, **values)


def with_changes(message_type: MessageType, **changes) -> dict:
    return {**SAMPLES[message_type][2], **changes}


def test_every_message_type_has_exactly_one_payload_model() -> None:
    assert set(PAYLOAD_MODELS) == set(MessageType)
    models = list(PAYLOAD_MODELS.values())
    assert len(set(models)) == len(models)
    assert set(MESSAGE_TYPE_BY_MODEL) == set(models)
    assert set(SAMPLES) == set(MessageType)


@pytest.mark.parametrize("model", list(PAYLOAD_MODELS.values()), ids=lambda m: m.__name__)
def test_payload_models_are_frozen_and_forbid_extra(model: type[BaseModel]) -> None:
    assert model.model_config.get("frozen") is True
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("message_type", list(MessageType), ids=str)
def test_payload_round_trips_through_json(message_type: MessageType) -> None:
    message = make(message_type)
    payload = validate_message(message)
    assert isinstance(payload, PAYLOAD_MODELS[message_type])

    wire = payload.model_dump(mode="json")
    again = validate_payload(message.model_copy(update={"payload": wire}))
    assert again == payload
    assert again.model_dump(mode="json") == wire
    assert type(payload).model_validate_json(payload.model_dump_json()) == payload

    rebuilt = build_message(message.source_agent, message.target_agent, payload)
    assert rebuilt.message_type is message_type
    assert rebuilt.payload == wire


@pytest.mark.parametrize("message_type", list(MessageType), ids=str)
def test_unknown_fields_are_rejected(message_type: MessageType) -> None:
    with pytest.raises(InvalidPayload) as exc_info:
        validate_payload(make(message_type, with_changes(message_type, surprise=True)))
    assert exc_info.value.message_type is message_type


@pytest.mark.parametrize("message_type", list(MessageType), ids=str)
def test_payload_for_another_type_is_rejected(message_type: MessageType) -> None:
    other = MessageType.TASK_TRANSITION
    if message_type is other:
        other = MessageType.BLOCKER
    with pytest.raises(InvalidPayload):
        validate_payload(make(message_type, SAMPLES[other][2]))


def test_invalid_payload_error_never_echoes_values() -> None:
    secret = "sk-test-not-a-real-secret"
    with pytest.raises(InvalidPayload) as exc_info:
        validate_payload(make(MessageType.TASK_RESULT, {"task_id": secret, "status": "done"}))
    assert secret not in str(exc_info.value)
    assert "task_id" in str(exc_info.value)


def test_unknown_message_type_is_rejected(monkeypatch) -> None:
    monkeypatch.delitem(PAYLOAD_MODELS, MessageType.HEALTH_EVENT)
    with pytest.raises(UnknownMessageType):
        validate_payload(make(MessageType.HEALTH_EVENT))


def test_datetimes_must_be_timezone_aware_and_are_normalised_to_utc() -> None:
    naive = with_changes(MessageType.HEALTH_EVENT, observed_at="2026-01-01T00:00:00")
    with pytest.raises(InvalidPayload):
        validate_payload(make(MessageType.HEALTH_EVENT, naive))

    offset = datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    aware = with_changes(MessageType.HEALTH_EVENT, observed_at=offset.isoformat())
    payload = validate_payload(make(MessageType.HEALTH_EVENT, aware))
    assert payload.observed_at == NOW
    assert payload.observed_at.utcoffset() == timedelta(0)


def test_payloads_are_immutable() -> None:
    payload = validate_payload(make(MessageType.TASK_ASSIGN))
    with pytest.raises(ValidationError):
        payload.priority = 1


# ---------------------------------------------------------------------------------- semantics


@pytest.mark.parametrize(
    ("message_type", "changes"),
    [
        (MessageType.OBJECTIVE_CREATE, {"stopping_conditions": []}),
        (MessageType.OBJECTIVE_CREATE, {"priority": 101}),
        (MessageType.OBJECTIVE_UPDATE, {"priority": None}),
        (MessageType.OBJECTIVE_UPDATE, {"status": "completed"}),
        (
            MessageType.OBJECTIVE_COMPLETED,
            {"criteria_results": [{"criterion": "works", "passed": False}]},
        ),
        (MessageType.OBJECTIVE_COMPLETED, {"criteria_results": []}),
        (MessageType.TASK_RESULT, {"status": "failed"}),
        (MessageType.TASK_TRANSITION, {"to_state": "exploded"}),
        (MessageType.RESEARCH_REQUEST, {"mode": "random"}),
        (MessageType.RESEARCH_REQUEST, {"budget": {"max_llm_requests": -1, "max_sources": 1}}),
        (MessageType.RESEARCH_FINDING, {"confidence": 1.5}),
        (MessageType.RESEARCH_FINDING, {"classification": "rumour"}),
        (MessageType.RESEARCH_FINDING, {"evidence": []}),
        (MessageType.RESEARCH_CONSOLIDATED, {"decisions": []}),
        (
            MessageType.RESEARCH_CONSOLIDATED,
            {
                "decisions": [
                    {"finding_id": str(FINDING), "decision": "adopt", "rationale": "a"},
                    {"finding_id": str(FINDING), "decision": "reject", "rationale": "b"},
                ]
            },
        ),
        (
            MessageType.RESEARCH_CONSOLIDATED,
            {"decisions": [{"finding_id": str(FINDING), "decision": "reject", "rationale": " "}]},
        ),
        (MessageType.REQUIREMENT_SUBMIT, {"requirement_type": "wish"}),
        (MessageType.REQUIREMENT_EVALUATED, {"acceptance_criteria": []}),
        (MessageType.REQUIREMENT_EVALUATED, {"rationale": ""}),
        (MessageType.SECURITY_EVALUATE, {"input_hash": HASH}),
        (MessageType.SECURITY_EVALUATE, {"requested_by": "SCOUT"}),
        (MessageType.SECURITY_EVALUATE, {"task_id": str(uuid4())}),
        (MessageType.SECURITY_DECISION, {"decision": "maybe"}),
        (MessageType.SECURITY_DECISION, {"input_hash": "not-a-hash"}),
        (MessageType.AUTHORIZATION_REQUEST, {"expires_at": "2026-01-01T00:00:00"}),
        (MessageType.USER_RESPONSE, {"authorization_request_id": None}),
        (MessageType.IMPLEMENTATION_PLAN, {"steps": []}),
        (MessageType.IMPLEMENTATION_PLAN, {"acceptance_criteria": []}),
        (MessageType.EXECUTION_REQUEST, {"requirement_id": None}),
        (MessageType.EXECUTION_REQUEST, {"acceptance_criteria": []}),
        (MessageType.EXECUTION_RESULT, {"status": "running"}),
        (MessageType.EXECUTION_RESULT, {"status": "failed"}),
        (MessageType.VERIFICATION_REQUEST, {"requirement_id": None}),
        (MessageType.TEST_REPORT, {"acceptance_results": []}),
        (MessageType.TEST_REPORT, {"results": {"passed": 3, "failed": 1}}),
        (MessageType.TEST_REPORT, {"verdict": "failed"}),
        (MessageType.CAPABILITY_REGISTER, {"verification_evidence": []}),
        (MessageType.CAPABILITY_REGISTER, {"verification_status": "unverified"}),
        (MessageType.CAPABILITY_REGISTER, {"lifecycle_status": "available"}),
        (MessageType.BLOCKER, {"route_to": "MUSE"}),
        (MessageType.BLOCKER, {"kind": "boredom"}),
        (MessageType.PROGRESS_UPDATE, {"percent_complete": 150}),
        (MessageType.HEALTH_EVENT, {"status": "fine"}),
        (MessageType.COMMUNICATION_REQUEST, {"authorization_request_id": None}),
        (MessageType.COMMUNICATION_DELIVERED, {"rendered_text_hash": "abc"}),
    ],
)
def test_invalid_payloads_are_rejected(message_type: MessageType, changes: dict) -> None:
    with pytest.raises(InvalidPayload):
        validate_payload(make(message_type, with_changes(message_type, **changes)))


def test_plain_json_is_required_for_free_form_fields() -> None:
    payload = with_changes(MessageType.TASK_RESULT, outputs={"value": float("nan")})
    with pytest.raises(InvalidPayload):
        validate_payload(make(MessageType.TASK_RESULT, payload))


def test_user_created_objectives_need_no_stopping_conditions() -> None:
    payload = ObjectiveCreatePayload(title="Learn my calendar habits")
    assert payload.generated is False
    assert payload.priority == 50


def test_research_finding_defaults_are_conservative() -> None:
    finding = ResearchFindingPayload(
        question="q", summary="s", confidence=0.2, classification="hypothesis", submitted_by="SCOUT"
    )
    assert finding.evidence == []
    with pytest.raises(ValidationError):
        ResearchFindingPayload(
            question="q", summary="s", confidence=0.2, classification="hypothesis",
            submitted_by="FORGE",
        )  # fmt: skip


def test_blocker_can_always_escalate_to_orchestr() -> None:
    for kind in contracts.BlockerKind:
        blocker = BlockerPayload(task_id=TASK, kind=kind, description="d", route_to="ORCHESTR")
        assert blocker.route_to is AgentId.ORCHESTR


def test_task_outcome_mirrors_task_result_status() -> None:
    assert {item.value for item in TaskOutcome} == {item.value for item in TaskResultStatus}


# ----------------------------------------------------------------------------------- envelope


def test_envelope_ids_must_match_payload_ids() -> None:
    assert validate_payload(make(MessageType.TASK_ASSIGN, task_id=TASK))
    with pytest.raises(InvalidPayload, match="task_id"):
        validate_payload(make(MessageType.TASK_ASSIGN, task_id=uuid4()))
    with pytest.raises(InvalidPayload, match="objective_id"):
        validate_payload(make(MessageType.TASK_ASSIGN, objective_id=uuid4()))


def test_task_assign_must_target_the_assigned_agent() -> None:
    with pytest.raises(InvalidPayload, match="assigned agent"):
        validate_payload(make(MessageType.TASK_ASSIGN, target_agent=AgentId.SCOUT))


def test_finding_submitter_must_be_the_sender() -> None:
    with pytest.raises(InvalidPayload, match="submitted_by"):
        validate_payload(make(MessageType.RESEARCH_FINDING, source_agent=AgentId.SCOUT))


def test_security_evaluate_must_come_from_the_requesting_agent() -> None:
    with pytest.raises(InvalidPayload):
        validate_payload(make(MessageType.SECURITY_EVALUATE, source_agent=AgentId.SCOUT))


def test_warden_cannot_request_review_of_its_own_operation() -> None:
    own = operation(AgentId.WARDEN)
    payload = {"operation": own.model_dump(mode="json"), "input_hash": own.input_hash}
    with pytest.raises(InvalidPayload):
        validate_payload(make(MessageType.SECURITY_EVALUATE, payload))


class _RecordingBus:
    def __init__(self) -> None:
        self.messages: list[AgentMessage] = []

    async def publish(self, message: AgentMessage) -> bool:
        self.messages.append(message)
        return True


async def test_existing_security_review_messages_satisfy_the_contract() -> None:
    bus = _RecordingBus()
    ctx = AgentContext(sessions=None, bus=bus)  # type: ignore[arg-type]
    result = await request_security_review(operation(), ctx)

    assert result.status is TaskResultStatus.WAITING_FOR_APPROVAL
    [message] = bus.messages
    payload = validate_message(message)
    assert payload.input_hash == operation().input_hash
    assert payload.block_reason == "approval_missing"


# -------------------------------------------------------------------------- silence and authz


def test_silence_never_authorizes() -> None:
    assert authorization_granted(None, AUTH_REQUEST) is False
    no_decision = UserResponsePayload(authorization_request_id=AUTH_REQUEST, text="hmm")
    assert no_decision.decision is contracts.UserDecision.NONE
    assert authorization_granted(no_decision, AUTH_REQUEST) is False
    denied = UserResponsePayload(authorization_request_id=AUTH_REQUEST, decision="denied")
    assert authorization_granted(denied, AUTH_REQUEST) is False


def test_only_an_explicit_approval_of_the_same_request_authorizes() -> None:
    approved = UserResponsePayload(authorization_request_id=AUTH_REQUEST, decision="approved")
    assert authorization_granted(approved, AUTH_REQUEST) is True
    assert authorization_granted(approved, uuid4()) is False


def test_decisions_require_an_authorization_request() -> None:
    with pytest.raises(ValidationError):
        UserResponsePayload(objective_id=OBJECTIVE, decision="approved")
    with pytest.raises(ValidationError):
        UserResponsePayload(text="unattached reply")
    assert UserResponsePayload(objective_id=OBJECTIVE, text="focus on voice first")


# ------------------------------------------------------------------------------------- routes

AGENTS = {definition.agent_id for definition in AGENT_DEFINITIONS}


def test_routes_cover_every_message_type_with_real_agents() -> None:
    assert set(ROUTES) == set(MessageType)
    assert set(SENDERS) == set(MessageType)
    for message_type in MessageType:
        assert ROUTES[message_type], f"{message_type} has no receiver"
        assert SENDERS[message_type], f"{message_type} has no sender"
        assert ROUTES[message_type] <= AGENTS
        assert SENDERS[message_type] <= AGENTS


def test_routes_are_reachable() -> None:
    # Every type has at least one sender/receiver pair that is not the same lone agent.
    for message_type in MessageType:
        pairs = {(s, r) for s in SENDERS[message_type] for r in ROUTES[message_type] if s != r}
        assert pairs, f"{message_type} can only be sent to oneself"


@pytest.mark.parametrize("message_type", list(MessageType), ids=str)
def test_samples_follow_the_routes(message_type: MessageType) -> None:
    check_route(make(message_type))


@pytest.mark.parametrize(
    ("message_type", "receivers"),
    [
        (MessageType.RESEARCH_REQUEST, {AgentId.SCOUT, AgentId.ROGUE}),
        (MessageType.RESEARCH_FINDING, {AgentId.SCOUT}),
        (MessageType.SECURITY_EVALUATE, {AgentId.WARDEN}),
        (MessageType.EXECUTION_REQUEST, {AgentId.FORGE}),
        (MessageType.VERIFICATION_REQUEST, {AgentId.LEDGER}),
        (MessageType.COMMUNICATION_REQUEST, {AgentId.MUSE}),
        (MessageType.OBJECTIVE_CREATE, {AgentId.ORCHESTR}),
        (
            MessageType.BLOCKER,
            {AgentId.ORCHESTR, AgentId.SCOUT, AgentId.ROGUE, AgentId.FORGE},
        ),
    ],
)
def test_specified_receivers(message_type: MessageType, receivers: set[AgentId]) -> None:
    assert ROUTES[message_type] == receivers


def test_critical_senders_are_restricted() -> None:
    assert SENDERS[MessageType.SECURITY_DECISION] == {AgentId.WARDEN}
    assert AgentId.WARDEN not in SENDERS[MessageType.SECURITY_EVALUATE]
    assert SENDERS[MessageType.CAPABILITY_REGISTER] == {AgentId.LEDGER}
    assert SENDERS[MessageType.TEST_REPORT] == {AgentId.LEDGER}
    assert SENDERS[MessageType.TASK_ASSIGN] == {AgentId.ORCHESTR}
    assert SENDERS[MessageType.COMMUNICATION_REQUEST] == {AgentId.ORCHESTR}
    assert SENDERS[MessageType.USER_RESPONSE] == {AgentId.MUSE}


def test_check_route_rejects_wrong_sender_and_receiver() -> None:
    with pytest.raises(RouteViolation, match="may not send"):
        check_route(make(MessageType.SECURITY_DECISION, source_agent=AgentId.FORGE))
    with pytest.raises(RouteViolation, match="may not receive"):
        check_route(make(MessageType.EXECUTION_REQUEST, target_agent=AgentId.SCOUT))


def test_build_message_enforces_route_and_envelope() -> None:
    payload = validate_payload(make(MessageType.TASK_ASSIGN))
    with pytest.raises(RouteViolation):
        build_message(AgentId.SCOUT, AgentId.FORGE, payload)
    with pytest.raises(InvalidPayload):
        build_message(AgentId.ORCHESTR, AgentId.FORGE, payload, task_id=uuid4())
    message = build_message(AgentId.ORCHESTR, AgentId.FORGE, payload, task_id=TASK, priority=90)
    assert message.priority == 90


def test_receivable_and_sendable_types_follow_the_tables() -> None:
    assert receivable_types(AgentId.WARDEN) == {
        MessageType.TASK_ASSIGN,
        MessageType.REQUIREMENT_EVALUATED,
        MessageType.SECURITY_EVALUATE,
        MessageType.USER_RESPONSE,
        MessageType.IMPLEMENTATION_PLAN,
    }
    for agent_id in AgentId:
        for message_type in receivable_types(agent_id):
            assert agent_id in ROUTES[message_type]
        for message_type in sendable_types(agent_id):
            assert agent_id in SENDERS[message_type]
    assert MessageType.SECURITY_DECISION in sendable_types(AgentId.WARDEN)
