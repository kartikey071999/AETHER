"""Deterministic, validated contracts for operational events."""

from datetime import UTC, datetime
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


class EventPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ObjectiveCreatedPayload(EventPayload):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    completion_criteria: list[str] = Field(default_factory=list)


class ObjectiveUpdatedPayload(EventPayload):
    changes: dict[str, Any] = Field(min_length=1)
    rationale: str


class TaskCreatedPayload(EventPayload):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    assigned_agent: AgentId | None = None


class TaskTransitionedPayload(EventPayload):
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class TaskAssignedPayload(EventPayload):
    assigned_agent: AgentId
    previous_agent: AgentId | None = None
    reason: str


class MessagePublishedPayload(EventPayload):
    message_id: UUID
    source_agent: AgentId
    target_agent: AgentId
    message_type: MessageType


class MessageDeliveredPayload(EventPayload):
    message_id: UUID
    target_agent: AgentId


class MessageProcessedPayload(MessageDeliveredPayload):
    outcome: str


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
    deferred: dict[UUID, str] = Field(default_factory=dict)


class AgentHeartbeatPayload(EventPayload):
    agent_id: AgentId
    status: str
    current_task_id: UUID | None = None


class ErrorRaisedPayload(EventPayload):
    error_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RecoveryAttemptedPayload(EventPayload):
    operation: str
    attempt: int = Field(ge=1)
    success: bool
    reason: str


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


EVENT_TYPES: dict[str, type[BaseModel]] = {
    "objective.created": ObjectiveCreatedPayload,
    "objective.updated": ObjectiveUpdatedPayload,
    "task.created": TaskCreatedPayload,
    "task.transitioned": TaskTransitionedPayload,
    "task.assigned": TaskAssignedPayload,
    "message.published": MessagePublishedPayload,
    "message.delivered": MessageDeliveredPayload,
    "message.processed": MessageProcessedPayload,
    "message.failed": MessageFailedPayload,
    "security.decision_recorded": SecurityDecisionPayload,
    "security.operation_blocked": SecurityOperationBlockedPayload,
    "execution.started": ExecutionStartedPayload,
    "execution.finished": ExecutionFinishedPayload,
    "test.run_finished": TestRunFinishedPayload,
    "capability.status_changed": CapabilityStatusChangedPayload,
    "resource.measured": ResourceMeasuredPayload,
    "scheduler.decision": SchedulerDecisionPayload,
    "agent.heartbeat": AgentHeartbeatPayload,
    "error.raised": ErrorRaisedPayload,
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
    }
)


class AetherEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1, max_length=64)
    category: EventCategory
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str = Field(min_length=1, max_length=200)
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
