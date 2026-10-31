"""SafeFiles: reads and writes confined to the policy's filesystem roots."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import SecurityPolicy
from aether.security.untrusted import UntrustedContent
from aether.tools.adapters._base import admit
from aether.tools.registry import OperationSpec


class FileReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    max_bytes: int | None = Field(default=None, ge=1)


class FileWriteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    content: str
    append: bool = False


@dataclass(frozen=True, slots=True)
class FileReadResult:
    path: Path
    content: UntrustedContent
    size: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileWriteResult:
    path: Path
    bytes_written: int
    sha256: str


class SafeFiles:
    kinds = frozenset({OperationKind.FILE_READ, OperationKind.FILE_WRITE})

    def __init__(self, permissions: PermissionService) -> None:
        self._permissions = permissions

    @property
    def _policy(self) -> SecurityPolicy:
        return self._permissions.policy

    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec:
        if kind is OperationKind.FILE_READ and isinstance(payload, FileReadInput):
            limit = self._policy.max_response_bytes
            return OperationSpec(
                target=payload.path,
                arguments={"max_bytes": min(payload.max_bytes or limit, limit)},
            )
        if kind is OperationKind.FILE_WRITE and isinstance(payload, FileWriteInput):
            return OperationSpec(
                target=payload.path,
                arguments={"content": payload.content, "append": payload.append},
            )
        raise TypeError(f"SafeFiles cannot describe {kind.value} with {type(payload).__name__}")

    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> FileReadResult | FileWriteResult:
        operation, grant = await admit(
            self._permissions, self.kinds, operation, authorization, correlation_id=correlation_id
        )
        path = self._policy.confine_path(operation.target)

        async def refuse(detail: str) -> Exception:
            return await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                detail,
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            )

        if path is None:
            raise await refuse("path is outside the allowed filesystem roots")
        if operation.kind is OperationKind.FILE_READ:
            limit = operation.arguments.get("max_bytes", self._policy.max_response_bytes)
            if not isinstance(limit, int) or not 0 < limit <= self._policy.max_response_bytes:
                raise await refuse("read limit is outside the policy maximum")
            limit = min(limit, grant.limits.max_output_bytes or limit)
            if not path.is_file():
                raise FileNotFoundError(f"{operation.target} is not a readable file")
            size = path.stat().st_size
            with path.open("rb") as handle:
                data = handle.read(limit)
            return FileReadResult(
                path=path,
                content=UntrustedContent(
                    source=f"file:{path}", content=data.decode("utf-8", errors="replace")
                ),
                size=size,
                truncated=size > len(data),
            )
        content = operation.arguments.get("content")
        append = operation.arguments.get("append", False)
        if not isinstance(content, str) or not isinstance(append, bool):
            raise await refuse("write arguments are malformed")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating parents so a raced symlink cannot redirect the write.
        if self._policy.confine_path(path) != path:
            raise await refuse("path changed during confinement checks")
        encoded = content.encode("utf-8")
        with path.open("ab" if append else "wb") as handle:
            handle.write(encoded)
        return FileWriteResult(
            path=path,
            bytes_written=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
