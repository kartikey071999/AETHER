"""Per-agent model facade: the only LLM access agents receive.

It exposes ``complete`` bound to one agent identity. It holds no public attribute leading to the
router, a provider or the budget guard, so agent code cannot complete as another agent, pick the
accounting identity, or reach a provider directly.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from aether.communication.messages import AgentId
from aether.models.contracts import CompletionRequest, CompletionResponse, ModelTier


class ModelUnavailable(RuntimeError):
    """No eligible model could serve the request (unavailable, over budget, or blocked)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("no eligible model: " + "; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    """The task/objective a completion is attributed to (for budgets and audit events)."""

    task_id: UUID | None = None
    objective_id: UUID | None = None


class AgentModels:
    __slots__ = ("_complete", "agent_id")

    agent_id: AgentId
    _complete: Callable[[CompletionRequest], Awaitable[CompletionResponse]]

    def __init__(
        self,
        agent_id: AgentId,
        complete: Callable[[CompletionRequest], Awaitable[CompletionResponse]],
    ) -> None:
        agent = AgentId(agent_id)

        async def routed(request: CompletionRequest) -> CompletionResponse:
            return await complete(request)

        object.__setattr__(self, "agent_id", agent)
        object.__setattr__(self, "_complete", routed)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AgentModels is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("AgentModels is read-only")

    def __repr__(self) -> str:
        return f"AgentModels(agent_id={self.agent_id.value!r})"

    async def complete(
        self, request: CompletionRequest, ctx: ModelCallContext | None = None
    ) -> CompletionResponse:
        """Complete as the bound agent; routing, budgets and authorization still apply.

        The agent and task/objective ids are always stamped here, overriding whatever the request
        carries. Deterministic work (``ModelTier.NONE``) must not reach an LLM.
        """
        if request.tier is ModelTier.NONE:
            raise ValueError("deterministic work must not call an LLM")
        call = ctx or ModelCallContext()
        stamped = request.model_copy(
            update={
                "agent": self.agent_id,
                "task_id": call.task_id,
                "objective_id": call.objective_id,
            }
        )
        return await self._complete(stamped)
