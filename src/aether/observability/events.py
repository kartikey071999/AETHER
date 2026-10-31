"""Deterministic, validated contracts for operational events."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    ValidationInfo,
    field_validator,
    model_validator,
)

from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus


class EventCategory(StrEnum):
    RUNTIME = "runtime"
    AGENT = "agent"
    COPILOT = "copilot"
    GIT = "git"
    SECURITY = "security"
    TEST = "test"
    RESOURCE = "resource"
    ERROR = "error"
    DECISION = "decision"
    OBJECTIVE = "objective"
    TASK = "task"
    MESSAGE = "message"
    CAPABILITY = "capability"
    REQUIREMENT = "requirement"
    RESEARCH = "research"
    AUTHORIZATION = "authorization"
    PERSONALIZATION = "personalization"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"


class EventPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ObjectiveCreatedPayload(EventPayload):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    completion_criteria: list[str] = Field(default_factory=list)
    parent_objective_id: UUID | None = None
    generated_by_agent: bool = False


class ObjectiveUpdatedPayload(EventPayload):
    changes: dict[str, Any] = Field(min_length=1)
    rationale: str


class ObjectivePriorityChangedPayload(EventPayload):
    from_priority: int = Field(ge=0, le=100)
    to_priority: int = Field(ge=0, le=100)
    reason: str


class ObjectiveStatusChangedPayload(EventPayload):
    from_status: str | None = None
    to_status: str
    reason: str | None = None


class TaskCreatedPayload(EventPayload):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    assigned_agent: AgentId | None = None
    objective_id: UUID | None = None
    parent_task_id: UUID | None = None
    requirement_id: UUID | None = None


class TaskTransitionedPayload(EventPayload):
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class TaskAssignedPayload(EventPayload):
    assigned_agent: AgentId
    previous_agent: AgentId | None = None
    reason: str


class TaskDependencyPayload(EventPayload):
    depends_on_task_id: UUID


class TaskLeaseAcquiredPayload(EventPayload):
    owner: str
    attempt: int = Field(ge=0)
    expires_at: datetime


class TaskLeaseReleasedPayload(EventPayload):
    owner: str
    final_status: TaskStatus


class TaskLeaseExpiredPayload(EventPayload):
    owner: str | None = None
    expired_at: datetime | None = None


class TaskOutcomePayload(EventPayload):
    """Final disposition of one task attempt (completed, failed, timed out, interrupted...)."""

    attempt: int | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=0)
    error: str | None = None
    will_retry: bool | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class MessagePublishedPayload(EventPayload):
    message_id: UUID
    source_agent: AgentId
    target_agent: AgentId
    message_type: MessageType


class MessageDeliveredPayload(EventPayload):
    message_id: UUID
    target_agent: AgentId
    message_type: str | None = None
    consumer: str | None = None


class MessageProcessingPayload(MessageDeliveredPayload):
    pass


class MessageRequeuedPayload(MessageDeliveredPayload):
    previous_status: str


class MessageProcessedPayload(MessageDeliveredPayload):
    outcome: str = "processed"


class MessageFailedPayload(MessageDeliveredPayload):
    error: str
    attempt: int = Field(default=1, ge=1)


class SecurityDecisionPayload(EventPayload):
    operation: str
    decision: str
    rationale: str
    scope: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class SecurityOperationBlockedPayload(EventPayload):
    operation: str
    reason: str
    decision_id: UUID | None = None


class ExecutionStartedPayload(EventPayload):
    execution_id: UUID
    operation: str


class ExecutionFinishedPayload(EventPayload):
    execution_id: UUID
    success: bool
    duration_seconds: float = Field(ge=0)
    exit_code: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TestRunFinishedPayload(EventPayload):
    suite: str
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(default=0, ge=0)
    duration_seconds: float = Field(ge=0)
    evidence: list[str] = Field(default_factory=list)


class CapabilityStatusChangedPayload(EventPayload):
    capability_id: UUID
    previous_status: str
    status: str
    reason: str


class ResourceMeasuredPayload(EventPayload):
    cpu_percent: float = Field(ge=0, le=100)
    available_ram_bytes: int = Field(ge=0)
    free_disk_bytes: int = Field(ge=0)


class SchedulerDecisionPayload(EventPayload):
    selected_task_id: UUID | None = None
    reason: str
    score: float | None = None
    deferred: dict[UUID, str] = Field(default_factory=dict)


class AgentHeartbeatPayload(EventPayload):
    agent_id: str = Field(min_length=1)
    status: str
    current_task_id: UUID | None = None
    heartbeat_id: UUID | None = None


class AgentRegisteredPayload(EventPayload):
    agent_id: str = Field(min_length=1)
    enabled: bool
    model_preference: str | None = None


class AgentEnabledPayload(EventPayload):
    agent_id: str = Field(min_length=1)


class AgentStatusChangedPayload(EventPayload):
    agent_id: str = Field(min_length=1)
    from_status: str | None = None
    to_status: str
    heartbeat_id: UUID | None = None
    current_task_id: UUID | None = None


class ErrorRaisedPayload(EventPayload):
    error_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorRecordedPayload(EventPayload):
    error_type: str
    message: str
    aggregate_type: str | None = None
    aggregate_id: UUID | None = None


class ErrorRecoveryAttemptedPayload(EventPayload):
    recovery_attempt_id: UUID
    action: str
    outcome: str
    resolved: bool


class ErrorResolvedPayload(EventPayload):
    note: str | None = None


class RecoveryAttemptedPayload(EventPayload):
    operation: str
    attempt: int = Field(ge=0)
    success: bool
    reason: str
    max_attempts: int | None = Field(default=None, ge=0)
    previous_owner: str | None = None


class DecisionRecordedPayload(EventPayload):
    decision: str
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class CopilotSessionStartedPayload(EventPayload):
    model: str
    prompt_summary: str


class CopilotSessionFinishedPayload(EventPayload):
    outcome: str
    duration_seconds: float = Field(ge=0)


class CopilotReportReceivedPayload(EventPayload):
    report: dict[str, Any]
    ready_for_integration: bool


class GitBranchCreatedPayload(EventPayload):
    branch: str
    base_branch: str


class GitCommitCreatedPayload(EventPayload):
    commit_hash: str = Field(min_length=1)
    message: str
    files: list[str] = Field(default_factory=list)


class GitPrOpenedPayload(EventPayload):
    number: int = Field(ge=1)
    url: str
    head_branch: str
    base_branch: str


class GitMergeCompletedPayload(EventPayload):
    source_branch: str
    target_branch: str
    commit_hash: str = Field(min_length=1)


class ProviderRequestCompletedPayload(EventPayload):
    provider: str
    model: str
    tokens: int = Field(ge=0)
    cost: float = Field(ge=0)
    latency_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    status: str = "succeeded"
    attempt: int = Field(default=1, ge=1)
    error_type: str | None = None
    usage_estimated: bool = False
    cost_estimate_unknown: bool = False


class ProviderUsageRecordedPayload(EventPayload):
    provider: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    status: str


class CapabilityDiscoveredPayload(EventPayload):
    name: str
    provider: str | None = None
    version: str | None = None


class CapabilityTransitionedPayload(EventPayload):
    from_status: str
    to_status: str
    details: dict[str, Any] = Field(default_factory=dict)


class RequirementCreatedPayload(EventPayload):
    title: str
    requirement_type: str
    source_finding_id: UUID | None = None
    security_review_required: bool = False


class RequirementStatusChangedPayload(EventPayload):
    from_status: str
    to_status: str
    reason: str | None = None


class FindingSubmittedPayload(EventPayload):
    question: str
    classification: str
    confidence: float = Field(ge=0, le=1)
    evidence_count: int = Field(ge=0)


class FindingEvidenceAddedPayload(EventPayload):
    evidence_id: UUID
    source_uri: str | None = None
    trust_level: str | None = None


class FindingStatusChangedPayload(EventPayload):
    from_status: str
    to_status: str
    rationale: str | None = None


class AuthorizationRequestedPayload(EventPayload):
    question: str
    security_decision_id: UUID | None = None
    expires_at: datetime | None = None


class AuthorizationAnsweredPayload(EventPayload):
    response: str | None = None


class AuthorizationExpiredPayload(EventPayload):
    expires_at: datetime | None = None


class PersonCreatedPayload(EventPayload):
    relationship: str | None = None
    access_level: str


class PersonDeletedPayload(EventPayload):
    pass


class PreferenceSetPayload(EventPayload):
    person_id: UUID
    name: str
    previous: Any = None
    value: Any = None
    source: str


class InteractionRecordedPayload(EventPayload):
    person_id: UUID | None = None
    channel: str
    direction: str
    authorization_request_id: UUID | None = None


class KnowledgeEntityPayload(EventPayload):
    entity_type: str
    name: str


class KnowledgeRelationCreatedPayload(EventPayload):
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: str


class RuntimeStartedPayload(EventPayload):
    agents: list[str] = Field(default_factory=list)
    owner: str
    recovery: dict[str, Any] = Field(default_factory=dict)
    spool_replayed: int | None = Field(default=None, ge=0)
    spool_failed: int | None = Field(default=None, ge=0)


class RuntimeStoppingPayload(EventPayload):
    running_tasks: int = Field(ge=0)


class RuntimeStoppedPayload(EventPayload):
    released_leases: int = Field(ge=0)


EVENT_TYPES: dict[str, type[BaseModel]] = {
    "objective.created": ObjectiveCreatedPayload,
    "objective.updated": ObjectiveUpdatedPayload,
    "objective.priority_changed": ObjectivePriorityChangedPayload,
    "objective.status_changed": ObjectiveStatusChangedPayload,
    "task.created": TaskCreatedPayload,
    "task.transitioned": TaskTransitionedPayload,
    "task.assigned": TaskAssignedPayload,
    "task.dependency_added": TaskDependencyPayload,
    "task.dependency_removed": TaskDependencyPayload,
    "task.lease_acquired": TaskLeaseAcquiredPayload,
    "task.lease_released": TaskLeaseReleasedPayload,
    "task.lease_expired": TaskLeaseExpiredPayload,
    "task.completed": TaskOutcomePayload,
    "task.blocked": TaskOutcomePayload,
    "task.waiting_for_approval": TaskOutcomePayload,
    "task.failed": TaskOutcomePayload,
    "task.timed_out": TaskOutcomePayload,
    "task.cancelled": TaskOutcomePayload,
    "task.interrupted": TaskOutcomePayload,
    "message.published": MessagePublishedPayload,
    "message.delivered": MessageDeliveredPayload,
    "message.processing": MessageProcessingPayload,
    "message.requeued": MessageRequeuedPayload,
    "message.processed": MessageProcessedPayload,
    "message.failed": MessageFailedPayload,
    "security.decision_recorded": SecurityDecisionPayload,
    "security.operation_blocked": SecurityOperationBlockedPayload,
    "execution.started": ExecutionStartedPayload,
    "execution.finished": ExecutionFinishedPayload,
    "test.run_finished": TestRunFinishedPayload,
    "capability.status_changed": CapabilityStatusChangedPayload,
    "capability.discovered": CapabilityDiscoveredPayload,
    "capability.transitioned": CapabilityTransitionedPayload,
    "requirement.created": RequirementCreatedPayload,
    "requirement.status_changed": RequirementStatusChangedPayload,
    "finding.submitted": FindingSubmittedPayload,
    "finding.evidence_added": FindingEvidenceAddedPayload,
    "finding.status_changed": FindingStatusChangedPayload,
    "authorization.requested": AuthorizationRequestedPayload,
    "authorization.approved": AuthorizationAnsweredPayload,
    "authorization.denied": AuthorizationAnsweredPayload,
    "authorization.expired": AuthorizationExpiredPayload,
    "person.created": PersonCreatedPayload,
    "person.deleted": PersonDeletedPayload,
    "preference.set": PreferenceSetPayload,
    "interaction.recorded": InteractionRecordedPayload,
    "kg.entity_created": KnowledgeEntityPayload,
    "kg.entity_updated": KnowledgeEntityPayload,
    "kg.relation_created": KnowledgeRelationCreatedPayload,
    "provider.usage_recorded": ProviderUsageRecordedPayload,
    "runtime.started": RuntimeStartedPayload,
    "runtime.stopping": RuntimeStoppingPayload,
    "runtime.stopped": RuntimeStoppedPayload,
    "resource.measured": ResourceMeasuredPayload,
    "scheduler.decision": SchedulerDecisionPayload,
    "agent.heartbeat": AgentHeartbeatPayload,
    "agent.registered": AgentRegisteredPayload,
    "agent.updated": AgentRegisteredPayload,
    "agent.enabled": AgentEnabledPayload,
    "agent.disabled": AgentEnabledPayload,
    "agent.status_changed": AgentStatusChangedPayload,
    "error.raised": ErrorRaisedPayload,
    "error.recorded": ErrorRecordedPayload,
    "error.recovery_attempted": ErrorRecoveryAttemptedPayload,
    "error.resolved": ErrorResolvedPayload,
    "recovery.attempted": RecoveryAttemptedPayload,
    "decision.recorded": DecisionRecordedPayload,
    "copilot.session_started": CopilotSessionStartedPayload,
    "copilot.session_finished": CopilotSessionFinishedPayload,
    "copilot.report_received": CopilotReportReceivedPayload,
    "git.branch_created": GitBranchCreatedPayload,
    "git.commit_created": GitCommitCreatedPayload,
    "git.pr_opened": GitPrOpenedPayload,
    "git.merge_completed": GitMergeCompletedPayload,
    "provider.request_completed": ProviderRequestCompletedPayload,
}

EVENT_CATEGORIES: dict[str, EventCategory] = {
    name: EventCategory(name.partition(".")[0])
    for name in EVENT_TYPES
    if name.partition(".")[0] in EventCategory
}
EVENT_CATEGORIES.update(
    {
        "execution.started": EventCategory.RUNTIME,
        "execution.finished": EventCategory.RUNTIME,
        "scheduler.decision": EventCategory.DECISION,
        "recovery.attempted": EventCategory.RUNTIME,
        "provider.request_completed": EventCategory.RESOURCE,
        "finding.submitted": EventCategory.RESEARCH,
        "finding.evidence_added": EventCategory.RESEARCH,
        "finding.status_changed": EventCategory.RESEARCH,
        "person.created": EventCategory.PERSONALIZATION,
        "person.deleted": EventCategory.PERSONALIZATION,
        "preference.set": EventCategory.PERSONALIZATION,
        "interaction.recorded": EventCategory.PERSONALIZATION,
        "kg.entity_created": EventCategory.KNOWLEDGE,
        "kg.entity_updated": EventCategory.KNOWLEDGE,
        "kg.relation_created": EventCategory.KNOWLEDGE,
    }
)


class AetherEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1, max_length=64)
    category: EventCategory
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(min_length=1, max_length=64)
    actor: AgentId | str = Field(min_length=1, max_length=24)
    aggregate_type: str = Field(min_length=1, max_length=32)
    aggregate_id: UUID
    objective_id: UUID | None = None
    task_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    session_id: UUID | None = None
    branch: str | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    payload: SerializeAsAny[EventPayload]

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("event timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any, info: ValidationInfo) -> BaseModel:
        event_type = info.data.get("event_type")
        payload_type = EVENT_TYPES.get(event_type)
        if payload_type is None:
            raise ValueError(f"unknown event type: {event_type}")
        return payload_type.model_validate(value)

    @model_validator(mode="after")
    def validate_category(self) -> "AetherEvent":
        if self.category != EVENT_CATEGORIES[self.event_type]:
            raise ValueError(f"incorrect category for event type: {self.event_type}")
        return self


def build_event(event_type: str, **kwargs: Any) -> AetherEvent:
    """Build a registered event, inferring its category unless supplied explicitly."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    kwargs.setdefault("category", EVENT_CATEGORIES[event_type])
    return AetherEvent(event_type=event_type, **kwargs)


# --- security / tools events (T-020) ---


class SecurityOperationBlockedHashedPayload(SecurityOperationBlockedPayload):
    input_hash: str | None = Field(default=None, min_length=64, max_length=64)


class SecurityOperationAuthorizedPayload(EventPayload):
    operation: str
    kind: str
    input_hash: str = Field(min_length=64, max_length=64)
    grant_id: UUID
    expires_at: datetime
    decision_id: UUID | None = None
    user_request_id: UUID | None = None


class SecurityUserAuthorizationRequestedPayload(EventPayload):
    operation: str
    input_hash: str = Field(min_length=64, max_length=64)
    request_id: UUID
    decision_id: UUID
    question: str


EVENT_TYPES.update(
    {
        "security.operation_blocked": SecurityOperationBlockedHashedPayload,
        "security.operation_authorized": SecurityOperationAuthorizedPayload,
        "security.user_authorization_requested": SecurityUserAuthorizationRequestedPayload,
    }
)
EVENT_CATEGORIES.update(
    {
        "security.operation_authorized": EventCategory.SECURITY,
        "security.user_authorization_requested": EventCategory.SECURITY,
    }
)


# --- security wiring (T-022) ---


class RuntimeSecurityInitializedPayload(EventPayload):
    """Non-secret summary of the security services built at startup."""

    policy_fingerprint: str = Field(min_length=64, max_length=64)
    tool_count: int = Field(ge=0)
    tools: list[str] = Field(default_factory=list)
    allowlist_sizes: dict[str, int] = Field(default_factory=dict)


EVENT_TYPES["runtime.security_initialized"] = RuntimeSecurityInitializedPayload
EVENT_CATEGORIES["runtime.security_initialized"] = EventCategory.RUNTIME


# --- model router wiring (T-024) ---


class RuntimeModelsInitializedPayload(EventPayload):
    """Non-secret summary of the model router built at startup: names, availability, counts."""

    mock: bool = False
    providers: list[dict[str, Any]] = Field(default_factory=list)
    model_count: int = Field(ge=0)
    max_concurrent_llm_requests: int = Field(ge=1)


EVENT_TYPES["runtime.models_initialized"] = RuntimeModelsInitializedPayload
EVENT_CATEGORIES["runtime.models_initialized"] = EventCategory.RUNTIME
