"""Typed descriptions of privileged operations and their stable review hash."""

import hashlib
import json
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aether.communication.messages import AgentId

# Namespace for deterministic per-operation audit aggregate IDs derived from the input hash.
_OPERATION_NAMESPACE = UUID("6f1d3c52-8a1e-4f0b-9a57-2c0e4f6b7d21")
_DESCRIPTION_LIMIT = 200


class OperationKind(StrEnum):
    SUBPROCESS = "subprocess"
    HTTP_REQUEST = "http_request"
    # Read-only GET of public research sources on the separate web research allowlist.
    WEB_FETCH = "web_fetch"
    PACKAGE_INSTALL = "package_install"
    REPO_CLONE = "repo_clone"
    FILE_WRITE = "file_write"
    FILE_READ = "file_read"
    LLM_CALL = "llm_call"
    CONFIG_CHANGE = "config_change"
    CREDENTIAL_ACCESS = "credential_access"
    NETWORK_SCAN = "network_scan"


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, no NaN/Infinity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def normalize_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


class Operation(BaseModel):
    """The exact operation under review; approvals bind to its ``input_hash``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OperationKind
    target: str = Field(min_length=1, max_length=4096)
    arguments: dict[str, Any] = Field(default_factory=dict)
    cwd: str | None = None
    hosts: tuple[str, ...] = ()
    requested_by: AgentId
    task_id: UUID | None = None
    objective_id: UUID | None = None
    declared_requirement_id: UUID | None = None

    @field_validator("arguments")
    @classmethod
    def _require_json_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        # A JSON round trip rejects non-JSON values and detaches the caller's mutable objects.
        try:
            return json.loads(canonical_json(value))
        except (TypeError, ValueError) as error:
            raise ValueError("operation arguments must be plain JSON data") from error

    @field_validator("hosts")
    @classmethod
    def _normalize_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        hosts = {normalize_host(host) for host in value}
        if "" in hosts:
            raise ValueError("operation hosts must not be empty")
        return tuple(sorted(hosts))

    def security_fields(self) -> dict[str, Any]:
        """Fields an approval is bound to. Task/objective IDs are context, not reviewed input."""
        return {
            "kind": self.kind.value,
            "target": self.target,
            "arguments": self.arguments,
            "cwd": self.cwd,
            "hosts": list(self.hosts),
            "requested_by": self.requested_by.value,
            "declared_requirement_id": (
                str(self.declared_requirement_id) if self.declared_requirement_id else None
            ),
        }

    @property
    def input_hash(self) -> str:
        # Recomputed on every access so any later in-place change invalidates bindings.
        return hashlib.sha256(canonical_json(self.security_fields()).encode("utf-8")).hexdigest()

    @property
    def aggregate_id(self) -> UUID:
        """Deterministic audit aggregate shared by every event about this exact operation."""
        return uuid5(_OPERATION_NAMESPACE, self.input_hash)

    def describe(self) -> str:
        description = f"{self.kind.value}:{self.target}"
        if len(description) > _DESCRIPTION_LIMIT:
            return description[: _DESCRIPTION_LIMIT - 3] + "..."
        return description
