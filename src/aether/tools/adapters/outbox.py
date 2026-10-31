"""Narrow outbox delivery: one new markdown file directly under ``data_dir/outbox``."""

import contextlib
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from aether.security.errors import BlockReason
from aether.security.operations import Operation, OperationKind
from aether.security.permissions import Authorization, PermissionService
from aether.security.policy import OUTBOX_ARGUMENT, OUTBOX_MAX_BYTES, SecurityPolicy
from aether.tools.adapters._base import admit
from aether.tools.registry import OperationSpec


class OutboxDeliveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=OUTBOX_MAX_BYTES)


@dataclass(frozen=True, slots=True)
class OutboxDeliveryResult:
    path: Path
    bytes_written: int
    sha256: str


class OutboxDelivery:
    """Creates a message file without ever overwriting, appending, or leaving the outbox."""

    kinds = frozenset({OperationKind.FILE_WRITE})

    def __init__(self, permissions: PermissionService) -> None:
        self._permissions = permissions

    @property
    def _policy(self) -> SecurityPolicy:
        return self._permissions.policy

    def describe(self, kind: OperationKind, payload: BaseModel) -> OperationSpec:
        if kind is OperationKind.FILE_WRITE and isinstance(payload, OutboxDeliveryInput):
            return OperationSpec(
                target=payload.path,
                arguments={"content": payload.content, OUTBOX_ARGUMENT: True},
            )
        raise TypeError(
            f"OutboxDelivery cannot describe {kind.value} with {type(payload).__name__}"
        )

    async def execute(
        self,
        operation: Operation,
        authorization: Authorization | None,
        *,
        correlation_id: UUID | None = None,
    ) -> OutboxDeliveryResult:
        operation, grant = await admit(
            self._permissions, self.kinds, operation, authorization, correlation_id=correlation_id
        )

        async def refuse(detail: str) -> Exception:
            return await self._permissions.reject(
                operation,
                BlockReason.POLICY_VIOLATION,
                detail,
                decision_id=grant.decision_id,
                correlation_id=correlation_id,
            )

        # ``admit`` already enforced the outbox policy (including read_only restrictions).
        path = Path(operation.target).resolve(strict=False)
        encoded = str(operation.arguments["content"]).encode("utf-8")
        outbox = self._policy.outbox_dir
        outbox.mkdir(parents=True, exist_ok=True)
        # Re-resolve after creating the directory so a raced symlink cannot redirect the write.
        if outbox.resolve(strict=True) != outbox or path.parent != outbox:
            raise await refuse("outbox directory changed during confinement checks")
        # A retry after a crash may find its own identical message; never replace anything.
        if not self._create_exclusive(path, encoded) and (
            not path.is_file() or path.is_symlink() or path.read_bytes() != encoded
        ):
            raise await refuse("an outbox file with different content already exists")
        return OutboxDeliveryResult(
            path=path, bytes_written=len(encoded), sha256=hashlib.sha256(encoded).hexdigest()
        )

    @staticmethod
    def _create_exclusive(path: Path, data: bytes) -> bool:
        """Atomically publish ``data`` at ``path``; ``False`` if ``path`` already exists."""
        staging = path.with_name(f".{uuid4().hex}.tmp")
        try:
            with staging.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staging, path)
            except FileExistsError:
                return False
            return True
        finally:
            with contextlib.suppress(OSError):
                staging.unlink()
