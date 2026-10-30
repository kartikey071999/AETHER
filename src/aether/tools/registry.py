"""Tool registry: the only path from an agent request to a privileged action.

``invoke`` validates input, builds the exact ``Operation``, obtains a single-use
``Authorization`` from the ``PermissionService``, and only then calls the adapter, which
re-verifies that authorization before acting.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from aether.communication.messages import AgentId
from aether.observability.events import EventCategory
from aether.observability.logging import get_logger
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService


class UnknownTool(LookupError):
    pass


class DuplicateTool(ValueError):
    pass


class ToolInputError(ValueError):
    """Tool arguments failed validation; nothing was authorized or executed."""


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """Security-relevant description an adapter derives from validated tool input."""

    target: str
    arguments: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolCallContext:
    task_id: UUID | None = None
    objective_id: UUID | None = None
    requirement_id: UUID | None = None
    correlation_id: UUID | None = None


class ToolAdapter(Protocol):
    kinds: frozenset[OperationKind]

    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec: ...

    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    kind: OperationKind
    input_model: type[BaseModel]
    adapter: ToolAdapter
    description: str = ""


class ToolRegistry:
    def __init__(self, permissions: PermissionService) -> None:
        self._permissions = permissions
        self._tools: dict[str, Tool] = {}

    @property
    def tools(self) -> Mapping[str, Tool]:
        return MappingProxyType(self._tools)

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("tool name must not be empty")
        if tool.name in self._tools:
            raise DuplicateTool(f"tool {tool.name!r} is already registered")
        if tool.kind not in tool.adapter.kinds:
            raise ValueError(f"adapter for {tool.name!r} does not support {tool.kind.value}")
        if not (isinstance(tool.input_model, type) and issubclass(tool.input_model, BaseModel)):
            raise TypeError("tool input_model must be a Pydantic model")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise UnknownTool(f"tool {name!r} is not registered") from error

    def build_operation(
        self,
        agent_id: AgentId | str,
        tool: str,
        args: Mapping[str, Any],
        ctx: ToolCallContext | None = None,
    ) -> Operation:
        """The exact operation ``invoke`` would authorize; WARDEN reviews this before approval."""
        registered = self.get(tool)
        ctx = ctx or ToolCallContext()
        try:
            payload = registered.input_model.model_validate(dict(args))
            spec = registered.adapter.describe(registered.kind, payload)
            return Operation(
                kind=registered.kind,
                target=spec.target,
                arguments=spec.arguments,
                cwd=spec.cwd,
                hosts=spec.hosts,
                requested_by=AgentId(agent_id),
                task_id=ctx.task_id,
                objective_id=ctx.objective_id,
                declared_requirement_id=ctx.requirement_id,
            )
        except (ValidationError, ValueError, TypeError) as error:
            get_logger(EventCategory.SECURITY).warning(
                "tool.input_rejected",
                extra={"tool": tool, "agent": str(agent_id), "error": str(error)},
            )
            raise ToolInputError(f"invalid input for tool {tool!r}: {error}") from error

    async def invoke(
        self,
        agent_id: AgentId | str,
        tool: str,
        args: Mapping[str, Any],
        ctx: ToolCallContext | None = None,
    ) -> Any:
        ctx = ctx or ToolCallContext()
        operation = self.build_operation(agent_id, tool, args, ctx)
        authorization = await self._permissions.authorize(
            operation, correlation_id=ctx.correlation_id
        )
        get_logger(EventCategory.SECURITY).log(
            logging.DEBUG, "tool.invoking", extra={"tool": tool, "input_hash": operation.input_hash}
        )
        return await self.get(tool).adapter.execute(
            operation, authorization, correlation_id=ctx.correlation_id
        )
