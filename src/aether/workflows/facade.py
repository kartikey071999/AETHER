"""``AgentWorkflows``: bound to one agent; runs only workflows it owns that are runnable."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from aether.communication.messages import AgentId
from aether.skills.base import SkillContext
from aether.workflows.engine import RUNNABLE, WorkflowEngine, WorkflowResult
from aether.workflows.lifecycle import WorkflowLifecycle

if TYPE_CHECKING:
    from aether.agents.base import AgentContext


class WorkflowNotPermitted(PermissionError):
    pass


class AgentWorkflows:
    __slots__ = ("_agent", "_engine", "_lifecycle")

    def __init__(self, agent: AgentId, engine: WorkflowEngine) -> None:
        self._agent = agent
        self._engine = engine
        self._lifecycle = WorkflowLifecycle(engine)

    # -- authoring (any agent, own namespace) and review (roles enforced by the lifecycle) ---

    async def propose(self, definition: dict[str, Any], rationale: str) -> tuple[str, int, str]:
        return await self._lifecycle.propose(self._agent, definition, rationale)

    async def approve(self, name: str, version: int, rationale: str) -> None:
        await self._lifecycle.approve(self._agent, name, version, rationale)

    async def reject(self, name: str, version: int, reason: str) -> None:
        await self._lifecycle.reject(self._agent, name, version, reason)

    async def verify(
        self,
        name: str,
        version: int,
        ctx: "AgentContext",
        *,
        inputs: dict[str, Any],
        fixtures: dict[str, dict[str, Any]],
    ) -> bool:
        skill_ctx = SkillContext(agent_id=self._agent, agent_ctx=ctx)
        return await self._lifecycle.verify(
            self._agent, name, version, inputs=inputs, fixtures=fixtures, ctx=skill_ctx
        )

    async def disable(self, name: str, version: int, reason: str) -> None:
        await self._lifecycle.disable(self._agent, name, version, reason)

    @property
    def agent_id(self) -> AgentId:
        return self._agent

    async def run(
        self,
        name: str,
        inputs: dict[str, Any],
        ctx: "AgentContext",
        *,
        objective_id: UUID | None = None,
        task_id: UUID | None = None,
        correlation_id: UUID | None = None,
        idempotency_key: str | None = None,
        version: int | None = None,
    ) -> WorkflowResult:
        definition, status = await self._engine.load(name, version)
        if definition.owner is not self._agent:
            raise WorkflowNotPermitted(f"{self._agent.value} does not own workflow {name}")
        if status not in RUNNABLE:
            raise WorkflowNotPermitted(f"workflow {name} is {status}, not verified")
        skill_ctx = SkillContext(
            agent_id=self._agent,
            agent_ctx=ctx,
            objective_id=objective_id,
            task_id=task_id,
            correlation_id=correlation_id,
        )
        return await self._engine.run(
            definition, inputs, skill_ctx, idempotency_key=idempotency_key
        )
