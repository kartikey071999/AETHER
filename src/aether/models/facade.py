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


class AgentBudgetExceeded(ModelUnavailable):
    """This agent's per-objective LLM budget is spent or its circuit breaker is open.

    Agents must fall back to deterministic behaviour; retrying cannot succeed for the objective.
    """


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    """The task/objective a completion is attributed to (for budgets and audit events)."""

    task_id: UUID | None = None
    objective_id: UUID | None = None


UnusableReporter = Callable[[AgentId, ModelCallContext, str], Awaitable[None]]


async def _ignore_report(agent: AgentId, ctx: ModelCallContext, reason: str) -> None:
    return None


class AgentModels:
    __slots__ = ("_complete", "_report", "agent_id")

    agent_id: AgentId
    _complete: Callable[[CompletionRequest], Awaitable[CompletionResponse]]
    _report: UnusableReporter

    def __init__(
        self,
        agent_id: AgentId,
        complete: Callable[[CompletionRequest], Awaitable[CompletionResponse]],
        *,
        report_unusable: UnusableReporter | None = None,
    ) -> None:
        agent = AgentId(agent_id)

        async def routed(request: CompletionRequest) -> CompletionResponse:
            return await complete(request)

        reporter = report_unusable or _ignore_report

        async def report(agent: AgentId, ctx: ModelCallContext, reason: str) -> None:
            await reporter(agent, ctx, reason)

        object.__setattr__(self, "agent_id", agent)
        object.__setattr__(self, "_complete", routed)
        object.__setattr__(self, "_report", report)

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

    async def report_unusable(
        self, ctx: ModelCallContext | None = None, *, reason: str = "invalid_output"
    ) -> None:
        """Count a completion the agent could not use (e.g. invalid JSON) as a model failure.

        Repeated unusable output trips the per-objective circuit breaker for this agent.
        """
        await self._report(self.agent_id, ctx or ModelCallContext(), reason)
