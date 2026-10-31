"""Process-wide security services built once at startup and injected into every agent context."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.base import AgentContext
from aether.communication.messages import AgentId
from aether.config import AetherSettings
from aether.observability.recorder import EventRecorder
from aether.security.approvals import ApprovalService
from aether.security.permissions import PermissionService
from aether.security.policy import SecurityPolicy, SecuritySettings, load_security_policy
from aether.security.summary import policy_fingerprint
from aether.tools.adapters import build_default_registry
from aether.tools.registry import ToolRegistry

APPROVALS_SERVICE = "approvals"


@dataclass(frozen=True, slots=True)
class SecurityServices:
    """One policy, one permission service, one approval service and one tool registry."""

    policy: SecurityPolicy
    permissions: PermissionService
    approvals: ApprovalService
    tools: ToolRegistry

    @property
    def fingerprint(self) -> str:
        return policy_fingerprint(self.policy)

    def initialized_payload(self) -> dict[str, Any]:
        """Non-secret facts for ``runtime.security_initialized``: hashes and counts only."""
        policy = self.policy
        tools = sorted(self.tools.tools)
        return {
            "policy_fingerprint": self.fingerprint,
            "tool_count": len(tools),
            "tools": tools,
            "allowlist_sizes": {
                "executables": len(policy.allowed_executables),
                "hosts": len(policy.allowed_hosts),
                "filesystem_roots": len(policy.filesystem_roots),
            },
        }

    def context_for(self, agent_id: AgentId, base: AgentContext) -> AgentContext:
        """``base`` plus the shared enforcement services; only WARDEN receives the approvals."""
        services = dict(base.services)
        if agent_id is AgentId.WARDEN:
            services[APPROVALS_SERVICE] = self.approvals
        else:
            services.pop(APPROVALS_SERVICE, None)
        return replace(
            base, permission_service=self.permissions, tools=self.tools, services=services
        )

    def contexts_for(
        self, agent_ids: Iterable[AgentId], base: AgentContext
    ) -> dict[AgentId, AgentContext]:
        return {agent_id: self.context_for(agent_id, base) for agent_id in agent_ids}


def build_security_services(
    settings: AetherSettings,
    sessions: async_sessionmaker[AsyncSession],
    recorder: EventRecorder,
    *,
    security_settings: SecuritySettings | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> SecurityServices:
    """Build the default-deny policy from settings and the services that enforce it."""
    policy = load_security_policy(settings, security_settings)
    # Adapters refuse to run in or write to roots that do not exist yet.
    for directory in (policy.workspace_dir, policy.artifact_dir, policy.data_dir):
        directory.mkdir(parents=True, exist_ok=True)
    permissions = PermissionService(sessions, recorder, policy)
    approvals = ApprovalService(sessions, recorder, policy)
    tools = build_default_registry(permissions, sessions, recorder, http_transport=http_transport)
    return SecurityServices(
        policy=policy, permissions=permissions, approvals=approvals, tools=tools
    )
