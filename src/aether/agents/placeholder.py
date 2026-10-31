"""Inert stand-ins that keep the seven agents registered until their behaviour exists (S3)."""

from functools import cache

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.registry import AGENT_DEFINITIONS
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import TaskRecord


class PlaceholderAgent(BaseAgent):
    """Accepts no messages and refuses tasks, so unimplemented work is never reported as done."""

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        return None

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        return TaskResult(
            status=TaskResultStatus.FAILED,
            summary=f"{self.agent_id.value} behaviour is not implemented yet",
            error=f"{self.agent_id.value} is a placeholder agent",
            retryable=False,
        )


@cache
def placeholder_class(agent_id: AgentId) -> type[PlaceholderAgent]:
    """The (cached) placeholder class standing in for `agent_id`."""
    return type(f"Placeholder{agent_id.value.title()}", (PlaceholderAgent,), {"agent_id": agent_id})


def build_placeholder_agents() -> list[BaseAgent]:
    """One placeholder per canonical agent definition."""
    return [placeholder_class(definition.agent_id)() for definition in AGENT_DEFINITIONS]
