"""Blocker routing: which agent resolves which kind of blocker, and when to escalate."""

from dataclasses import dataclass

from aether.communication.contracts import BLOCKER_ROUTES, BlockerKind
from aether.communication.messages import AgentId

DEFAULT_ASSIGNEE: dict[BlockerKind, AgentId] = {
    BlockerKind.RESEARCH: AgentId.SCOUT,
    BlockerKind.IMPLEMENTATION: AgentId.FORGE,
    BlockerKind.SECURITY: AgentId.WARDEN,
    BlockerKind.AUTHORIZATION: AgentId.MUSE,
    BlockerKind.RESOURCE: AgentId.ORCHESTR,
}

_AGENT_KIND: dict[AgentId, BlockerKind] = {
    AgentId.SCOUT: BlockerKind.RESEARCH,
    AgentId.ROGUE: BlockerKind.RESEARCH,
    AgentId.FORGE: BlockerKind.IMPLEMENTATION,
    AgentId.WARDEN: BlockerKind.SECURITY,
    AgentId.MUSE: BlockerKind.AUTHORIZATION,
}


@dataclass(frozen=True, slots=True)
class BlockerRoute:
    assignee: AgentId | None
    creates_task: bool
    asks_user: bool
    escalate: bool


def infer_kind(assigned_agent: str | None) -> BlockerKind:
    """Kind for a blocked task result that did not say; unknown owners are resource blocks."""
    try:
        return _AGENT_KIND.get(AgentId(assigned_agent), BlockerKind.RESOURCE)
    except ValueError:
        return BlockerKind.RESOURCE


def route_blocker(
    kind: BlockerKind,
    *,
    attempts: int,
    max_attempts: int,
    route_to: AgentId | None = None,
) -> BlockerRoute:
    """Pick the resolver. Research escalates SCOUT -> ROGUE after a failed attempt."""
    if attempts >= max_attempts:
        return BlockerRoute(None, creates_task=False, asks_user=True, escalate=True)
    if kind is BlockerKind.AUTHORIZATION:
        return BlockerRoute(AgentId.MUSE, creates_task=False, asks_user=True, escalate=False)
    if kind is BlockerKind.RESOURCE:
        return BlockerRoute(AgentId.ORCHESTR, creates_task=False, asks_user=False, escalate=False)
    allowed = BLOCKER_ROUTES[kind]
    assignee = DEFAULT_ASSIGNEE[kind]
    if kind is BlockerKind.RESEARCH and attempts >= 1:
        assignee = AgentId.ROGUE
    if route_to is not None and route_to in allowed and attempts == 0:
        assignee = route_to
    return BlockerRoute(assignee, creates_task=True, asks_user=False, escalate=False)
