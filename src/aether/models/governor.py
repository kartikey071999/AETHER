"""Per-objective, per-agent LLM allowances and a consecutive-failure circuit breaker.

Daily provider budgets alone cannot stop one objective from burning its whole day: a loop of
failing drafts and re-plans stays under every per-request cap. The governor sits between the
agent facade and the router. For each (objective, agent) it tracks provider calls, charged
tokens and consecutive failures. It refuses further LLM use with ``AgentBudgetExceeded`` once
the allowance is spent or the breaker opens, so agents fall back to deterministic behaviour.

State is in memory and per process (bounded LRU). A restart resets it, which is acceptable
because every completion is still capped by the durable daily provider budgets.
"""

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId
from aether.models.budgets import RouteSpend
from aether.models.contracts import CompletionRequest, CompletionResponse
from aether.models.facade import AgentBudgetExceeded, ModelCallContext
from aether.models.settings import ProviderSettings
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder

logger = logging.getLogger("aether.models.governor")

Route = Callable[[CompletionRequest, RouteSpend], Awaitable[CompletionResponse]]


@dataclass(frozen=True)
class _Call:
    agent: str
    objective_id: UUID
    task_id: UUID | None


@dataclass
class _Ledger:
    requests: int = 0
    tokens: int = 0
    failures: int = 0
    failures_before_success: int = 0
    exhausted: str | None = None
    circuit_open: bool = False


class ModelGovernor:
    def __init__(
        self,
        settings: ProviderSettings,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        recorder: EventRecorder | None = None,
        max_entries: int = 4096,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.recorder = recorder or EventRecorder()
        self.max_entries = max_entries
        self._ledgers: OrderedDict[tuple[UUID, str], _Ledger] = OrderedDict()

    def usage(self, objective_id: UUID, agent: AgentId | str) -> tuple[int, int, int]:
        """(requests, tokens, consecutive failures) recorded for one objective and agent."""
        ledger = self._ledgers.get((objective_id, AgentId(agent).value), _Ledger())
        return ledger.requests, ledger.tokens, ledger.failures

    def _ledger(self, objective_id: UUID, agent: str) -> _Ledger:
        key = (objective_id, agent)
        ledger = self._ledgers.get(key)
        if ledger is None:
            ledger = self._ledgers[key] = _Ledger()
            while len(self._ledgers) > self.max_entries:
                self._ledgers.popitem(last=False)
        else:
            self._ledgers.move_to_end(key)
        return ledger

    async def complete(self, request: CompletionRequest, route: Route) -> CompletionResponse:
        if request.objective_id is None:
            # Unattributed calls (e.g. CLI probes) are bounded only by provider budgets.
            return await route(request, RouteSpend())
        agent = request.agent.value
        call = _Call(agent, request.objective_id, request.task_id)
        policy = self.settings.agent_budget(agent)
        ledger = self._ledger(request.objective_id, agent)
        if ledger.circuit_open:
            raise AgentBudgetExceeded([f"{agent}: circuit breaker open for this objective"])
        if ledger.exhausted is None:
            if (
                policy.max_requests_per_objective is not None
                and ledger.requests >= policy.max_requests_per_objective
            ):
                await self._exhaust(call, ledger, "objective request budget")
            elif (
                policy.max_tokens_per_objective is not None
                and ledger.tokens >= policy.max_tokens_per_objective
            ):
                await self._exhaust(call, ledger, "objective token budget")
        if ledger.exhausted is not None:
            raise AgentBudgetExceeded([f"{agent}: exceeded {ledger.exhausted}"])
        spend = RouteSpend(
            max_requests=(
                None
                if policy.max_requests_per_objective is None
                else policy.max_requests_per_objective - ledger.requests
            ),
            max_tokens=(
                None
                if policy.max_tokens_per_objective is None
                else policy.max_tokens_per_objective - ledger.tokens
            ),
        )
        try:
            response = await route(request, spend)
        except Exception as error:
            self._charge(ledger, spend)
            if spend.refusal is not None:
                await self._exhaust(call, ledger, spend.refusal)
                raise AgentBudgetExceeded([f"{agent}: exceeded {spend.refusal}"]) from None
            await self._failed(call, ledger, type(error).__name__)
            raise
        self._charge(ledger, spend)
        ledger.failures_before_success = ledger.failures
        ledger.failures = 0
        return response

    async def report_unusable(self, agent: AgentId, ctx: ModelCallContext, reason: str) -> None:
        """A completion the agent rejected counts as a failure, undoing the success reset."""
        if ctx.objective_id is None:
            return
        ledger = self._ledger(ctx.objective_id, AgentId(agent).value)
        ledger.failures = max(ledger.failures, ledger.failures_before_success)
        ledger.failures_before_success = 0
        await self._failed(
            _Call(AgentId(agent).value, ctx.objective_id, ctx.task_id),
            ledger,
            reason[:64] or "invalid_output",
        )

    @staticmethod
    def _charge(ledger: _Ledger, spend: RouteSpend) -> None:
        ledger.requests += spend.requests
        ledger.tokens += spend.tokens

    async def _failed(self, call: _Call, ledger: _Ledger, error: str) -> None:
        ledger.failures += 1
        threshold = self.settings.circuit_breaker_failures
        if threshold and ledger.failures >= threshold and not ledger.circuit_open:
            ledger.circuit_open = True
            await self._record(
                "provider.circuit_opened",
                call,
                {
                    "agent": call.agent,
                    "consecutive_failures": ledger.failures,
                    "threshold": threshold,
                    "last_error": error,
                },
            )

    async def _exhaust(self, call: _Call, ledger: _Ledger, reason: str) -> None:
        if ledger.exhausted is not None:
            return
        ledger.exhausted = reason
        policy = self.settings.agent_budget(call.agent)
        await self._record(
            "provider.budget_exhausted",
            call,
            {
                "agent": call.agent,
                "reason": reason,
                "requests": ledger.requests,
                "tokens": ledger.tokens,
                "max_requests": policy.max_requests_per_objective,
                "max_tokens": policy.max_tokens_per_objective,
            },
        )

    async def _record(self, event_type: str, call: _Call, payload: dict) -> None:
        logger.warning("%s: agent=%s objective=%s", event_type, call.agent, call.objective_id)
        if self.sessions is None:
            return
        try:
            await self.recorder.record(
                self.sessions,
                build_event(
                    event_type,
                    source="aether.models",
                    actor=call.agent,
                    aggregate_type="objective",
                    aggregate_id=call.objective_id,
                    objective_id=call.objective_id,
                    task_id=call.task_id,
                    idempotency_key=f"{event_type}/{call.objective_id}/{call.agent}",
                    payload=payload,
                ),
            )
        except Exception as error:  # noqa: BLE001 - audit failure must not re-enable the LLM
            logger.error("could not record %s: %s", event_type, type(error).__name__)
