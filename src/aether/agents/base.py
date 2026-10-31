"""Shared agent interface: identity, permissions, message/task handling, and health."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.registry import AgentDefinition, get_agent
from aether.agents.terminal import ignore_if_terminal
from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.storage.models import TaskRecord

if TYPE_CHECKING:
    from aether.models.facade import AgentModels
    from aether.tools.facade import AgentTools


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
    # Agents only ever see a read-only permission view and a facade bound to their own identity.
    permission_service: PermissionService | None = None
    tools: "AgentTools | None" = None
    models: "AgentModels | None" = None
    services: dict[str, Any] = field(default_factory=dict)


class TaskResultStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    # The task waits for an independent verification run (LEDGER), not for a person or WARDEN.
    WAITING_FOR_VERIFICATION = "waiting_for_verification"


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskResultStatus
    summary: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Outcome of `BaseAgent.invoke_tool`: the tool's value, or the result to return instead."""

    value: Any = None
    result: TaskResult | None = None

    @property
    def ok(self) -> bool:
        return self.result is None


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
    guard_terminal_objectives: ClassVar[bool] = False

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
        """Validate the message type, then hand it to the role-specific handler.

        Agents that create work from messages set ``guard_terminal_objectives``: work messages
        for a cancelled/completed/failed objective are then acknowledged and recorded only.
        """
        self.ensure_accepts(message)
        if self.guard_terminal_objectives and await ignore_if_terminal(
            self.agent_id, message, ctx.sessions
        ):
            ctx.logger.info(
                "message ignored: objective is terminal",
                extra={
                    "agent": self.agent_id.value,
                    "message_type": message.message_type.value,
                    "message_id": str(message.message_id),
                },
            )
            return
        await self.handle_message(message, ctx)

    @abstractmethod
    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None: ...

    @abstractmethod
    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult: ...

    async def invoke_tool(
        self,
        tool: str,
        args: Mapping[str, Any],
        ctx: AgentContext,
        *,
        task: TaskRecord | None = None,
        correlation_id: UUID | None = None,
    ) -> ToolInvocation:
        """The only way an agent performs a privileged action; always acts as ``self.agent_id``.

        Blocked or failed calls come back as a structured ``TaskResult`` in ``.result`` for the
        caller to return from ``execute_task``; operations that need WARDEN review are routed to
        WARDEN and reported as ``waiting_for_approval``.
        """
        # Imported lazily: the security and tool packages import the agent registry.
        from aether.agents.security_review import blocked_result
        from aether.security.errors import OperationBlocked
        from aether.tools import ExecutionFailed, ToolCallContext, ToolInputError, UnknownTool

        if ctx.tools is None:
            return ToolInvocation(
                result=TaskResult(
                    status=TaskResultStatus.FAILED,
                    summary="no tool registry is available to this agent",
                    error="tool registry is not configured",
                    retryable=False,
                )
            )
        call = ToolCallContext(
            task_id=task.id if task else None,
            objective_id=task.objective_id if task else None,
            requirement_id=task.requirement_id if task else None,
            correlation_id=correlation_id,
        )
        try:
            value = await ctx.tools.invoke(tool, args, call)
        except OperationBlocked as blocked:
            operation = ctx.tools.build_operation(tool, args, call)
            return ToolInvocation(
                result=await blocked_result(blocked, operation, ctx, correlation_id=correlation_id)
            )
        except (ToolInputError, UnknownTool) as error:
            return ToolInvocation(
                result=TaskResult(
                    status=TaskResultStatus.FAILED,
                    summary=f"tool {tool!r} request was rejected",
                    error=str(error),
                    retryable=False,
                )
            )
        except ExecutionFailed as error:
            return ToolInvocation(
                result=TaskResult(
                    status=TaskResultStatus.FAILED,
                    summary=f"tool {tool!r} failed",
                    error=str(error),
                )
            )
        return ToolInvocation(value=value)

    async def cancel(self, task_id: UUID) -> None:
        """Request cancellation; long-running work should poll `is_cancelled`.

        The runtime also uses this signal for a cooperative pause of a running task.
        """
        self._cancelled.add(task_id)

    def is_cancelled(self, task_id: UUID) -> bool:
        return task_id in self._cancelled

    def clear_cancellation(self, task_id: UUID) -> None:
        """Forget a stop signal once the runtime has settled it (e.g. a paused task resumes)."""
        self._cancelled.discard(task_id)

    async def tick(self, ctx: AgentContext) -> Any:
        """Optional periodic hook the runtime calls on start and every agent tick interval.

        The default does nothing. Implementations must be idempotent and read all state from
        the database; the runtime bounds each call with a timeout and records failures.
        """
        return None

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
