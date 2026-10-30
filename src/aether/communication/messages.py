"""Structured, validated inter-agent message contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentId(StrEnum):
    ORCHESTR = "ORCHESTR"
    SCOUT = "SCOUT"
    ROGUE = "ROGUE"
    FORGE = "FORGE"
    LEDGER = "LEDGER"
    WARDEN = "WARDEN"
    MUSE = "MUSE"


class MessageType(StrEnum):
    OBJECTIVE_CREATE = "objective.create"
    OBJECTIVE_UPDATE = "objective.update"
    TASK_CREATE = "task.create"
    TASK_ASSIGN = "task.assign"
    TASK_TRANSITION = "task.transition"
    RESEARCH_FINDING = "research.finding"
    REQUIREMENT_SUBMIT = "requirement.submit"
    SECURITY_EVALUATE = "security.evaluate"
    SECURITY_DECISION = "security.decision"
    RESOURCE_REQUEST = "resource.request"
    IMPLEMENTATION_PLAN = "implementation.plan"
    EXECUTION_REQUEST = "execution.request"
    EXECUTION_RESULT = "execution.result"
    TEST_REPORT = "test.report"
    BLOCKER = "blocker"
    AUTHORIZATION_REQUEST = "authorization.request"
    CAPABILITY_REGISTER = "capability.register"
    PROGRESS_UPDATE = "progress.update"
    HEALTH_EVENT = "health.event"


class MessageStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class AgentMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: UUID = Field(default_factory=uuid4)
    source_agent: AgentId
    target_agent: AgentId
    message_type: MessageType
    objective_id: UUID | None = None
    task_id: UUID | None = None
    requirement_id: UUID | None = None
    correlation_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]
    priority: int = Field(default=50, ge=0, le=100)
    deadline: datetime | None = None
    requires_response: bool = False
    status: MessageStatus = MessageStatus.PENDING

    @field_validator("timestamp", "deadline")
    @classmethod
    def require_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value
