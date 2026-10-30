"""Shared agent interface: identity, permissions, message/task handling, and health."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.registry import AgentDefinition, get_agent
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.storage.models import TaskRecord


class PermissionService(Protocol):
    """Placeholder for WARDEN-backed enforcement; injected so agents never import it directly."""

    async def is_allowed(self, agent_id: AgentId, action: str) -> bool: ...


@dataclass(slots=True)
class AgentContext:
    """Approved services handed to agents via dependency injection."""

    sessions: async_sessionmaker[AsyncSession]
    bus: MessageBus
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("aether.agent"))
    permission_service: PermissionService | None = None
    services: dict[str, Any] = field(default_factory=dict)


class TaskResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_FOR_APPROVAL = "waiting_for_approval"


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskResultStatus
    summary: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    retryable: bool = True


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class AgentHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: AgentId
    status: HealthStatus
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UnsupportedMessageType(Exception):
    """Structured rejection raised when an agent receives a message it does not accept."""

    def __init__(
        self, agent_id: AgentId, message_type: MessageType, accepted: frozenset[MessageType]
    ) -> None:
        self.agent_id = agent_id
        self.message_type = message_type
        self.accepted = accepted
        super().__init__(
            f"{agent_id.value} does not accept message type {message_type.value!r}; "
            f"accepted: {sorted(item.value for item in accepted)}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "unsupported_message_type",
            "agent_id": self.agent_id.value,
            "message_type": self.message_type.value,
            "accepted": sorted(item.value for item in self.accepted),
        }


class BaseAgent(ABC):
    agent_id: ClassVar[AgentId]
    accepted_message_types: ClassVar[frozenset[MessageType]] = frozenset()

    def __init__(self) -> None:
        self._started = False
        self._cancelled: set[UUID] = set()

    @property
    def definition(self) -> AgentDefinition:
        return get_agent(self.agent_id)

    @property
    def permissions(self) -> tuple[str, ...]:
        return self.definition.allowed_actions

    def ensure_accepts(self, message: AgentMessage) -> None:
        if message.message_type not in self.accepted_message_types:
            raise UnsupportedMessageType(
                self.agent_id, message.message_type, self.accepted_message_types
            )

    async def dispatch_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        """Validate the message type, then hand it to the role-specific handler."""
        self.ensure_accepts(message)
        await self.handle_message(message, ctx)

    @abstractmethod
    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None: ...

    @abstractmethod
    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult: ...

    async def cancel(self, task_id: UUID) -> None:
        """Request cancellation; long-running work should poll `is_cancelled`."""
        self._cancelled.add(task_id)

    def is_cancelled(self, task_id: UUID) -> bool:
        return task_id in self._cancelled

    async def heartbeat(self) -> AgentHealth:
        status = HealthStatus.HEALTHY if self._started else HealthStatus.UNHEALTHY
        detail = "running" if self._started else "not started"
        return AgentHealth(agent_id=self.agent_id, status=status, detail=detail)

    async def health_check(self) -> AgentHealth:
        return await self.heartbeat()

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
