"""Immutable, default-deny runtime security policy loaded once at startup."""

import os
import re
import shutil
from collections.abc import Iterable, Mapping
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.agents.registry import AGENT_DEFINITIONS, AgentDefinition
from aether.communication.messages import AgentId
from aether.security.errors import PolicyMutationError
from aether.security.operations import Operation, OperationKind, normalize_host

if TYPE_CHECKING:  # config imports the agent limit models, which import security
    from aether.config import AetherSettings

_K = OperationKind

# Agent ``allowed_actions`` (agents/registry.py) mapped to the operation kinds they permit.
# Actions that are not listed grant no privileged operation at all.
ACTION_OPERATION_KINDS: Mapping[str, frozenset[OperationKind]] = MappingProxyType(
    {
        "execution.request": frozenset(
            {
                _K.SUBPROCESS,
                _K.PACKAGE_INSTALL,
                _K.REPO_CLONE,
                _K.HTTP_REQUEST,
                _K.FILE_READ,
                _K.FILE_WRITE,
            }
        ),
        "verification.run": frozenset({_K.SUBPROCESS, _K.FILE_READ}),
        "records.read": frozenset({_K.FILE_READ}),
        "research.create": frozenset({_K.HTTP_REQUEST, _K.WEB_FETCH, _K.FILE_READ, _K.LLM_CALL}),
        "security.evaluate": frozenset({_K.HTTP_REQUEST, _K.FILE_READ}),
        "implementation.propose": frozenset({_K.FILE_READ, _K.LLM_CALL}),
        "communication.prepare": frozenset({_K.LLM_CALL}),
        # Only the narrow outbox delivery tool: see ``outbox_violation``.
        "communication.deliver": frozenset({_K.FILE_WRITE}),
        "objective.manage": frozenset({_K.LLM_CALL}),
    }
)

OUTBOX_DELIVERY_ACTION = "communication.deliver"
# Operation argument that only the outbox delivery tool sets; generic write_file never does.
OUTBOX_ARGUMENT = "outbox_delivery"
OUTBOX_DIRNAME = "outbox"
OUTBOX_MAX_BYTES = 256 * 1024
OUTBOX_FILENAME = re.compile(
    r"^\d{8}T\d{6}Z-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.md$"
)

# Kinds that can never run without both a WARDEN approval and explicit user authorization.
ALWAYS_REVIEWED_KINDS = frozenset({_K.CONFIG_CHANGE, _K.CREDENTIAL_ACCESS})
# Kinds that must name the remote host(s) they contact.
NETWORK_KINDS = frozenset(
    {_K.HTTP_REQUEST, _K.WEB_FETCH, _K.REPO_CLONE, _K.NETWORK_SCAN, _K.LLM_CALL}
)
FILE_KINDS = frozenset({_K.FILE_READ, _K.FILE_WRITE})

# ``web_fetch`` is deliberately absent: it is a bounded, read-only GET confined to the separate
# web research allowlist, and its output is untrusted data. Add it here to require WARDEN review.
DEFAULT_APPROVAL_REQUIRED_KINDS = (
    _K.SUBPROCESS,
    _K.HTTP_REQUEST,
    _K.PACKAGE_INSTALL,
    _K.REPO_CLONE,
    _K.CONFIG_CHANGE,
    _K.CREDENTIAL_ACCESS,
    _K.NETWORK_SCAN,
)
DEFAULT_USER_AUTHORIZATION_KINDS = (_K.CONFIG_CHANGE, _K.CREDENTIAL_ACCESS, _K.NETWORK_SCAN)

_SCP_LIKE = re.compile(r"^[\w.-]+@(?P<host>[\w.-]+):(?!//)")


def permitted_kinds(definition: AgentDefinition) -> frozenset[OperationKind]:
    kinds: set[OperationKind] = set()
    for action in definition.allowed_actions:
        kinds |= ACTION_OPERATION_KINDS.get(action, frozenset())
    return frozenset(kinds)


def default_agent_permissions() -> Mapping[AgentId, frozenset[OperationKind]]:
    return MappingProxyType(
        {definition.agent_id: permitted_kinds(definition) for definition in AGENT_DEFINITIONS}
    )


def outbox_writers() -> frozenset[AgentId]:
    """Agents declared with the narrow outbox delivery action."""
    return frozenset(
        definition.agent_id
        for definition in AGENT_DEFINITIONS
        if OUTBOX_DELIVERY_ACTION in definition.allowed_actions
    )


def outbox_only_writers() -> frozenset[AgentId]:
    """Outbox writers whose only source of ``file_write`` is outbox delivery."""
    scoped = set()
    for definition in AGENT_DEFINITIONS:
        if definition.agent_id not in outbox_writers():
            continue
        others = [a for a in definition.allowed_actions if a != OUTBOX_DELIVERY_ACTION]
        if not any(_K.FILE_WRITE in ACTION_OPERATION_KINDS.get(a, frozenset()) for a in others):
            scoped.add(definition.agent_id)
    return frozenset(scoped)


def url_host(target: str) -> str | None:
    """Host of a URL or scp-style git remote, or ``None`` when ``target`` is neither."""
    match = _SCP_LIKE.match(target)
    if match:
        return normalize_host(match["host"])
    try:
        parts = urlsplit(target)
    except ValueError:
        return None
    if parts.scheme and parts.hostname:
        return normalize_host(parts.hostname)
    return None


def host_matches(host: str, allowlist: frozenset[str]) -> bool:
    host = normalize_host(host)
    if not host:
        return False
    for allowed in allowlist:
        if allowed.startswith("*."):
            if host.endswith(allowed[1:]) and host != allowed[2:]:
                return True
        elif host == allowed:
            return True
    return False


def _normcase(path: Path) -> str:
    return os.path.normcase(str(path))


def _resolve_path(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _resolve_executable(entry: str | Path) -> Path | None:
    candidate = Path(entry)
    if not candidate.is_absolute():
        found = shutil.which(str(entry))
        if found is None:
            return None
        candidate = Path(found)
    resolved = _resolve_path(candidate)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


class SecuritySettings(BaseSettings):
    """Startup configuration for the security policy (``AETHER_SECURITY_*``, JSON lists)."""

    model_config = SettingsConfigDict(
        env_prefix="AETHER_SECURITY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    allowed_executables: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    extra_filesystem_roots: list[Path] = Field(default_factory=list)
    approval_required_kinds: list[OperationKind] = Field(
        default_factory=lambda: list(DEFAULT_APPROVAL_REQUIRED_KINDS)
    )
    user_authorization_required_kinds: list[OperationKind] = Field(
        default_factory=lambda: list(DEFAULT_USER_AUTHORIZATION_KINDS)
    )
    max_approval_ttl_seconds: int = Field(default=3600, ge=1, le=7 * 24 * 3600)
    authorization_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    subprocess_timeout_seconds: float = Field(default=300.0, gt=0, le=24 * 3600)
    max_output_bytes: int = Field(default=1024 * 1024, ge=1)
    http_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_response_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    max_redirects: int = Field(default=5, ge=0, le=20)


class SecurityPolicy:
    """Read-only policy. There is deliberately no API to change it after construction.

    Every attribute assignment or deletion raises ``PolicyMutationError``; containers are
    frozensets, tuples, or ``MappingProxyType`` views. A new policy requires a restart.
    """

    __slots__ = (
        "agent_permissions",
        "allowed_executables",
        "allowed_hosts",
        "approval_required_kinds",
        "artifact_dir",
        "authorization_ttl",
        "data_dir",
        "filesystem_roots",
        "http_timeout_seconds",
        "max_approval_ttl",
        "max_output_bytes",
        "max_redirects",
        "max_response_bytes",
        "outbox_dir",
        "subprocess_timeout_seconds",
        "user_authorization_required_kinds",
        "web_allowed_hosts",
        "workspace_dir",
    )

    agent_permissions: Mapping[AgentId, frozenset[OperationKind]]
    allowed_executables: frozenset[str]
    allowed_hosts: frozenset[str]
    approval_required_kinds: frozenset[OperationKind]
    artifact_dir: Path
    authorization_ttl: timedelta
    data_dir: Path
    filesystem_roots: tuple[Path, ...]
    http_timeout_seconds: float
    max_approval_ttl: timedelta
    max_output_bytes: int
    max_redirects: int
    max_response_bytes: int
    outbox_dir: Path
    subprocess_timeout_seconds: float
    user_authorization_required_kinds: frozenset[OperationKind]
    web_allowed_hosts: frozenset[str]
    workspace_dir: Path

    def __init__(
        self,
        *,
        workspace_dir: str | Path,
        artifact_dir: str | Path,
        data_dir: str | Path,
        extra_filesystem_roots: Iterable[str | Path] = (),
        allowed_executables: Iterable[str | Path] = (),
        allowed_hosts: Iterable[str] = (),
        web_allowed_hosts: Iterable[str] = (),
        approval_required_kinds: Iterable[OperationKind] = DEFAULT_APPROVAL_REQUIRED_KINDS,
        user_authorization_required_kinds: Iterable[OperationKind] = (
            DEFAULT_USER_AUTHORIZATION_KINDS
        ),
        agent_permissions: Mapping[AgentId, Iterable[OperationKind]] | None = None,
        max_approval_ttl: timedelta = timedelta(hours=1),
        authorization_ttl: timedelta = timedelta(seconds=60),
        subprocess_timeout_seconds: float = 300.0,
        max_output_bytes: int = 1024 * 1024,
        http_timeout_seconds: float = 30.0,
        max_response_bytes: int = 5 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        if max_approval_ttl <= timedelta(0) or authorization_ttl <= timedelta(0):
            raise ValueError("approval and authorization lifetimes must be positive")
        if subprocess_timeout_seconds <= 0 or http_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if max_output_bytes < 1 or max_response_bytes < 1 or max_redirects < 0:
            raise ValueError("output limits must be positive")
        directories = {}
        for name, value in (
            ("workspace_dir", workspace_dir),
            ("artifact_dir", artifact_dir),
            ("data_dir", data_dir),
        ):
            resolved = _resolve_path(value)
            if resolved is None:
                raise ValueError(f"{name} cannot be resolved")
            directories[name] = resolved
        roots: list[Path] = list(directories.values())
        for root in extra_filesystem_roots:
            resolved = _resolve_path(root)
            if resolved is None:
                raise ValueError(f"filesystem root {root!s} cannot be resolved")
            roots.append(resolved)
        executables = {
            _normcase(path)
            for path in (_resolve_executable(entry) for entry in allowed_executables)
            if path is not None
        }
        approval = frozenset(OperationKind(kind) for kind in approval_required_kinds)
        user = frozenset(OperationKind(kind) for kind in user_authorization_required_kinds)
        user |= ALWAYS_REVIEWED_KINDS
        # User authorization is the second gate after WARDEN, so it implies WARDEN review.
        approval |= user
        permissions = (
            default_agent_permissions()
            if agent_permissions is None
            else {
                AgentId(agent): frozenset(OperationKind(kind) for kind in kinds)
                for agent, kinds in agent_permissions.items()
            }
        )
        values: dict[str, Any] = {
            **directories,
            "outbox_dir": directories["data_dir"] / OUTBOX_DIRNAME,
            "filesystem_roots": tuple(dict.fromkeys(roots)),
            "allowed_executables": frozenset(executables),
            "allowed_hosts": frozenset(normalize_host(host) for host in allowed_hosts if host),
            "web_allowed_hosts": frozenset(
                normalize_host(host) for host in web_allowed_hosts if host
            ),
            "approval_required_kinds": approval,
            "user_authorization_required_kinds": user,
            "agent_permissions": MappingProxyType(dict(permissions)),
            "max_approval_ttl": max_approval_ttl,
            "authorization_ttl": authorization_ttl,
            "subprocess_timeout_seconds": float(subprocess_timeout_seconds),
            "max_output_bytes": int(max_output_bytes),
            "http_timeout_seconds": float(http_timeout_seconds),
            "max_response_bytes": int(max_response_bytes),
            "max_redirects": int(max_redirects),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_settings(
        cls,
        settings: "AetherSettings",
        security: SecuritySettings | None = None,
        *,
        web_allowed_hosts: Iterable[str] = (),
    ) -> "SecurityPolicy":
        security = security or SecuritySettings()
        return cls(
            workspace_dir=settings.workspace_dir,
            artifact_dir=settings.artifact_dir,
            data_dir=settings.data_dir,
            extra_filesystem_roots=security.extra_filesystem_roots,
            allowed_executables=security.allowed_executables,
            allowed_hosts=security.allowed_hosts,
            web_allowed_hosts=web_allowed_hosts,
            approval_required_kinds=security.approval_required_kinds,
            user_authorization_required_kinds=security.user_authorization_required_kinds,
            max_approval_ttl=timedelta(seconds=security.max_approval_ttl_seconds),
            authorization_ttl=timedelta(seconds=security.authorization_ttl_seconds),
            subprocess_timeout_seconds=security.subprocess_timeout_seconds,
            max_output_bytes=security.max_output_bytes,
            http_timeout_seconds=security.http_timeout_seconds,
            max_response_bytes=security.max_response_bytes,
            max_redirects=security.max_redirects,
        )

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise PolicyMutationError("the security policy is immutable at runtime")

    def __delattr__(self, name: str) -> NoReturn:
        raise PolicyMutationError("the security policy is immutable at runtime")

    def __repr__(self) -> str:
        return (
            f"SecurityPolicy(roots={len(self.filesystem_roots)}, "
            f"executables={len(self.allowed_executables)}, hosts={len(self.allowed_hosts)}, "
            f"web_hosts={len(self.web_allowed_hosts)})"
        )

    def permits(self, agent_id: AgentId, kind: OperationKind) -> bool:
        return kind in self.agent_permissions.get(agent_id, frozenset())

    def permits_operation(self, operation: Operation) -> bool:
        """Kind-level permission, narrowed for agents limited to outbox delivery."""
        if not self.permits(operation.requested_by, operation.kind):
            return False
        return not (
            operation.kind is OperationKind.FILE_WRITE
            and OUTBOX_ARGUMENT not in operation.arguments
            and operation.requested_by in outbox_only_writers()
        )

    def requires_approval(self, kind: OperationKind) -> bool:
        return kind in self.approval_required_kinds

    def requires_user_authorization(self, kind: OperationKind) -> bool:
        return kind in self.user_authorization_required_kinds

    def resolve_executable(self, target: str) -> Path | None:
        """Absolute, symlink-resolved path of an allowlisted executable, else ``None``."""
        if not target or "\x00" in target:
            return None
        resolved = _resolve_executable(target)
        if resolved is None or _normcase(resolved) not in self.allowed_executables:
            return None
        return resolved

    def confine_path(self, path: str | Path) -> Path | None:
        """Resolve ``path`` (relative paths start at the workspace) inside an allowed root."""
        if "\x00" in str(path):
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        resolved = _resolve_path(candidate)
        if resolved is None:
            return None
        for root in self.filesystem_roots:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        return None

    def is_host_allowed(self, host: str) -> bool:
        return host_matches(host, self.allowed_hosts)

    def is_web_host_allowed(self, host: str) -> bool:
        """Whether ``host`` is on the separate read-only web research allowlist."""
        return host_matches(host, self.web_allowed_hosts)

    def host_allowed_for(self, kind: OperationKind, host: str) -> bool:
        """``web_fetch`` uses only the web research allowlist; every other kind the general one."""
        if kind is OperationKind.WEB_FETCH:
            return self.is_web_host_allowed(host)
        return self.is_host_allowed(host)

    def outbox_violation(self, operation: Operation) -> str | None:
        """Why ``operation`` is not a valid single-file outbox delivery, else ``None``."""
        if operation.kind is not OperationKind.FILE_WRITE:
            return "outbox delivery must be a file write"
        if operation.requested_by not in outbox_writers():
            return f"{operation.requested_by.value} may not deliver outbox messages"
        target = operation.target
        if "\x00" in target or not Path(target).is_absolute():
            return "outbox path must be an absolute path"
        resolved = _resolve_path(target)
        if resolved is None or resolved.parent != self.outbox_dir:
            return "outbox delivery must write one file directly in the outbox directory"
        if Path(target).name != resolved.name or not OUTBOX_FILENAME.match(resolved.name):
            return "outbox file name must match <timestamp>-<uuid>.md"
        content = operation.arguments.get("content")
        if set(operation.arguments) != {"content", OUTBOX_ARGUMENT} or not isinstance(content, str):
            return "outbox delivery arguments are malformed"
        if operation.arguments[OUTBOX_ARGUMENT] is not True:
            return "outbox delivery arguments are malformed"
        size = len(content.encode("utf-8"))
        if not 0 < size <= OUTBOX_MAX_BYTES:
            return "outbox message size is outside the allowed range"
        return None

    def static_violation(self, operation: Operation) -> str | None:
        """First allowlist/confinement violation of ``operation``, or ``None`` if compliant."""
        if OUTBOX_ARGUMENT in operation.arguments:
            violation = self.outbox_violation(operation)
            if violation is not None:
                return violation
        elif operation.kind is OperationKind.FILE_WRITE and (
            operation.requested_by in outbox_only_writers()
        ):
            return "agent may only write through outbox delivery"
        if operation.cwd is not None and self.confine_path(operation.cwd) is None:
            return "working directory is outside the allowed filesystem roots"
        hosts = set(operation.hosts)
        if operation.kind in NETWORK_KINDS:
            target_host = url_host(operation.target)
            if target_host is not None:
                hosts.add(target_host)
            if not hosts:
                return "network operation does not declare its remote host"
        for host in sorted(hosts):
            if not self.host_allowed_for(operation.kind, host):
                return f"host {host!r} is not allowlisted"
        if (
            operation.kind is OperationKind.SUBPROCESS
            and self.resolve_executable(operation.target) is None
        ):
            return "executable is not allowlisted"
        if operation.kind in FILE_KINDS and self.confine_path(operation.target) is None:
            return "path is outside the allowed filesystem roots"
        return None


def load_security_policy(
    settings: "AetherSettings",
    security: SecuritySettings | None = None,
    *,
    web_allowed_hosts: Iterable[str] = (),
) -> SecurityPolicy:
    """Build the process-wide policy once at startup; changing it requires a restart."""
    return SecurityPolicy.from_settings(settings, security, web_allowed_hosts=web_allowed_hosts)
