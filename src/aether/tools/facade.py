"""Per-agent tool facade: the only tool access agents receive.

It exposes tool descriptions and an ``invoke`` bound to one agent identity. It holds no public
attribute leading to the registry, a ``Tool`` or an adapter, so agent code cannot invoke a tool
as another agent or reach a privileged adapter directly.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from aether.communication.messages import AgentId
from aether.security.operations import Operation, OperationKind
from aether.tools.registry import ToolCallContext, ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """Public description of a tool: name, kind and input schema only."""

    name: str
    kind: OperationKind
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class AgentTools:
    __slots__ = ("_build_operation", "_invoke", "_list_tools", "agent_id")

    agent_id: AgentId
    _build_operation: Callable[[str, Mapping[str, Any], ToolCallContext | None], Operation]
    _invoke: Callable[[str, Mapping[str, Any], ToolCallContext | None], Any]
    _list_tools: Callable[[], tuple[ToolInfo, ...]]

    def __init__(self, agent_id: AgentId, registry: ToolRegistry) -> None:
        agent = AgentId(agent_id)

        def list_tools() -> tuple[ToolInfo, ...]:
            return tuple(
                ToolInfo(
                    name=tool.name,
                    kind=tool.kind,
                    description=tool.description,
                    input_schema=tool.input_model.model_json_schema(),
                )
                for _, tool in sorted(registry.tools.items())
            )

        async def invoke(
            tool: str, args: Mapping[str, Any], ctx: ToolCallContext | None = None
        ) -> Any:
            return await registry.invoke(agent, tool, args, ctx)

        def build_operation(
            tool: str, args: Mapping[str, Any], ctx: ToolCallContext | None = None
        ) -> Operation:
            return registry.build_operation(agent, tool, args, ctx)

        object.__setattr__(self, "agent_id", agent)
        object.__setattr__(self, "_list_tools", list_tools)
        object.__setattr__(self, "_invoke", invoke)
        object.__setattr__(self, "_build_operation", build_operation)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AgentTools is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("AgentTools is read-only")

    def __repr__(self) -> str:
        return f"AgentTools(agent_id={self.agent_id.value!r})"

    def list_tools(self) -> tuple[ToolInfo, ...]:
        return self._list_tools()

    async def invoke(
        self, tool: str, args: Mapping[str, Any], ctx: ToolCallContext | None = None
    ) -> Any:
        """Run ``tool`` as the bound agent; the registry still authorizes every call."""
        return await self._invoke(tool, args, ctx)

    def build_operation(
        self, tool: str, args: Mapping[str, Any], ctx: ToolCallContext | None = None
    ) -> Operation:
        """The exact operation ``invoke`` would authorize (pure; used to request WARDEN review)."""
        return self._build_operation(tool, args, ctx)
