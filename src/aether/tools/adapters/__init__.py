"""Safe execution adapters. Each refuses to act without a matching single-use Authorization."""

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.recorder import EventRecorder
from aether.security.operations import OperationKind
from aether.security.permissions import PermissionService
from aether.tools.adapters._base import ExecutionFailed
from aether.tools.adapters.files import FileReadInput, FileWriteInput, SafeFiles
from aether.tools.adapters.http import HttpInput, HttpResult, SafeHttp
from aether.tools.adapters.subprocess import (
    SafeSubprocess,
    SubprocessInput,
    SubprocessResult,
    kill_process_tree,
    scrub_environment,
)
from aether.tools.registry import Tool, ToolRegistry


def build_default_registry(
    permissions: PermissionService,
    sessions: async_sessionmaker[AsyncSession],
    recorder: EventRecorder,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> ToolRegistry:
    """Registry with the standard tools: run_process, http_request, read_file, write_file."""
    registry = ToolRegistry(permissions)
    files = SafeFiles(permissions)
    registry.register(
        Tool(
            "run_process",
            OperationKind.SUBPROCESS,
            SubprocessInput,
            SafeSubprocess(permissions, sessions, recorder),
            "Run an allowlisted executable with an argv list (no shell).",
        )
    )
    registry.register(
        Tool(
            "http_request",
            OperationKind.HTTP_REQUEST,
            HttpInput,
            SafeHttp(permissions, sessions, recorder, transport=http_transport),
            "Send an HTTP(S) request to an allowlisted host.",
        )
    )
    registry.register(
        Tool("read_file", OperationKind.FILE_READ, FileReadInput, files, "Read a confined file.")
    )
    registry.register(
        Tool(
            "write_file", OperationKind.FILE_WRITE, FileWriteInput, files, "Write a confined file."
        )
    )
    return registry


__all__ = [
    "ExecutionFailed",
    "FileReadInput",
    "FileWriteInput",
    "HttpInput",
    "HttpResult",
    "SafeFiles",
    "SafeHttp",
    "SafeSubprocess",
    "SubprocessInput",
    "SubprocessResult",
    "build_default_registry",
    "kill_process_tree",
    "scrub_environment",
]
