"""Tool registry and safe execution adapters behind the central permission service."""

from aether.tools.adapters._base import ExecutionFailed
from aether.tools.registry import (
    DuplicateTool,
    OperationSpec,
    Tool,
    ToolAdapter,
    ToolCallContext,
    ToolInputError,
    ToolRegistry,
    UnknownTool,
)

__all__ = [
    "DuplicateTool",
    "ExecutionFailed",
    "OperationSpec",
    "Tool",
    "ToolAdapter",
    "ToolCallContext",
    "ToolInputError",
    "ToolRegistry",
    "UnknownTool",
]
