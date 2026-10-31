"""Read-only view of the permission service for agent contexts: no way to obtain a grant."""

from typing import Any

from aether.communication.messages import AgentId
from aether.security.permissions import PermissionService


class PermissionView:
    """Exposes ``is_allowed`` only; ``authorize``/``redeem`` stay with the runtime tool path."""

    __slots__ = ("_is_allowed",)

    _is_allowed: Any

    def __init__(self, permissions: PermissionService) -> None:
        object.__setattr__(self, "_is_allowed", permissions.is_allowed)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("PermissionView is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("PermissionView is read-only")

    async def is_allowed(self, agent_id: AgentId, action: str) -> bool:
        return bool(await self._is_allowed(agent_id, action))
