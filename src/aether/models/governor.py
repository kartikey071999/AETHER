"""Per-objective, per-agent LLM allowances and a consecutive-failure circuit breaker.

Daily provider budgets alone cannot stop one objective from burning its whole day: a loop of
failing drafts and re-plans stays under every per-request cap. The governor sits between the
agent facade and the router. For each (objective, agent) it tracks provider calls, charged
tokens and consecutive failures. It refuses further LLM use with ``AgentBudgetExceeded`` once
the allowance is spent or the breaker opens, so agents fall back to deterministic behaviour.

Calls for one (objective, agent) are serialised, so concurrent work (e.g. a redelivered
assignment racing the task run) cannot spend the same allowance twice.

State is durable. The first time an (objective, agent) is used after a process start (or after
its bounded-LRU entry was evicted) the ledger is seeded from ``provider_usage`` (calls and tokens)
and the breaker is replayed from the latest ``provider.circuit_opened`` / ``provider.circuit_closed``
event, so a restart never hands an objective a fresh allowance. An open breaker is half-open once
``circuit_breaker_cooldown_seconds`` have passed: exactly one probe call is allowed; success closes
the breaker, failure re-opens it and restarts the cool-down. Consecutive-failure counts below the
threshold are not persisted.
"""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.communication.messages import AgentId
from aether.models.budgets import RouteSpend
from aether.models.contracts import CompletionRequest, CompletionResponse
from aether.models.facade import AgentBudgetExceeded, ModelCallContext
from aether.models.settings import ProviderSettings
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.storage.models import EventRecord
from aether.storage.repositories.provider_usage import ProviderUsageRepository

logger = logging.getLogger("aether.models.governor")

Route = Callable[[CompletionRequest, RouteSpend], Awaitable[CompletionResponse]]

CIRCUIT_OPENED = "provider.circuit_opened"
CIRCUIT_CLOSED = "provider.circuit_closed"


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
    opened_at: datetime | None = None
    probing: bool = False
    seeded: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ModelGovernor:
    def __init__(
        self,
        settings: ProviderSettings,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        recorder: EventRecorder | None = None,
        max_entries: int = 4096,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.recorder = recorder or EventRecorder()
        self.max_entries = max_entries
        self.clock = clock
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
        ledger = self._ledger(request.objective_id, agent)
        async with ledger.lock:
            return await self._governed(
                request, route, ledger, _Call(agent, request.objective_id, request.task_id)
            )

    async def _seed(self, call: _Call, ledger: _Ledger) -> None:
        """Load durable usage and breaker state once; fail closed if it cannot be read."""
        if ledger.seeded:
            return
        if self.sessions is None:
            ledger.seeded = True
            return
        try:
            requests, tokens = await ProviderUsageRepository(self.sessions).objective_agent_usage(
                call.objective_id, call.agent
            )
            async with self.sessions() as session:
                latest = (
                    await session.execute(
                        select(EventRecord.event_type, EventRecord.created_at, EventRecord.payload)
                        .where(
                            EventRecord.objective_id == call.objective_id,
                            EventRecord.actor == call.agent,
                            EventRecord.event_type.in_((CIRCUIT_OPENED, CIRCUIT_CLOSED)),
                        )
                        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
                        .limit(1)
                    )
                ).first()
        except Exception as error:  # noqa: BLE001 - an unreadable ledger must not mean "no usage"
            logger.error("could not load durable LLM usage: %s", type(error).__name__)
            raise AgentBudgetExceeded(
                [f"{call.agent}: durable LLM usage unavailable for this objective"]
            ) from None
        ledger.requests, ledger.tokens = requests, tokens
        threshold = self.settings.circuit_breaker_failures
        if latest is not None and latest.event_type == CIRCUIT_OPENED and threshold:
            ledger.circuit_open = True
            ledger.opened_at = _aware(latest.created_at)
            recorded = (latest.payload or {}).get("consecutive_failures")
            ledger.failures = max(
                ledger.failures, recorded if isinstance(recorded, int) else threshold
            )
        ledger.seeded = True

    def _cooled_down(self, ledger: _Ledger) -> bool:
        cooldown = timedelta(seconds=self.settings.circuit_breaker_cooldown_seconds)
        return ledger.opened_at is None or self.clock() - ledger.opened_at >= cooldown

    async def _governed(
        self, request: CompletionRequest, route: Route, ledger: _Ledger, call: _Call
    ) -> CompletionResponse:
        agent = call.agent
        policy = self.settings.agent_budget(agent)
        await self._seed(call, ledger)
        probe = False
        if ledger.circuit_open:
            if not self._cooled_down(ledger):
                raise AgentBudgetExceeded([f"{agent}: circuit breaker open for this objective"])
            # Half-open: this single serialised call decides whether the breaker closes.
            probe = True
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
        ledger.probing = probe
        try:
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
            if probe:
                await self._close(call, ledger)
            return response
        finally:
            ledger.probing = False

    async def report_unusable(self, agent: AgentId, ctx: ModelCallContext, reason: str) -> None:
        """A completion the agent rejected counts as a failure, undoing the success reset."""
        if ctx.objective_id is None:
            return
        call = _Call(AgentId(agent).value, ctx.objective_id, ctx.task_id)
        ledger = self._ledger(ctx.objective_id, call.agent)
        async with ledger.lock:
            try:
                await self._seed(call, ledger)
            except AgentBudgetExceeded:
                pass  # the next call retries seeding and fails closed
            ledger.failures = max(ledger.failures, ledger.failures_before_success)
            ledger.failures_before_success = 0
            await self._failed(call, ledger, reason[:64] or "invalid_output")

    @staticmethod
    def _charge(ledger: _Ledger, spend: RouteSpend) -> None:
        ledger.requests += spend.requests
        ledger.tokens += spend.tokens

    async def _failed(self, call: _Call, ledger: _Ledger, error: str) -> None:
        ledger.failures += 1
        threshold = self.settings.circuit_breaker_failures
        # A failed probe re-opens the breaker and restarts the cool-down.
        if (
            threshold
            and ledger.failures >= threshold
            and (not ledger.circuit_open or ledger.probing)
        ):
            ledger.circuit_open = True
            ledger.opened_at = self.clock()
            await self._record(
                CIRCUIT_OPENED,
                call,
                {
                    "agent": call.agent,
                    "consecutive_failures": ledger.failures,
                    "threshold": threshold,
                    "last_error": error,
                },
                timestamp=ledger.opened_at,
            )

    async def _close(self, call: _Call, ledger: _Ledger) -> None:
        ledger.circuit_open = False
        ledger.opened_at = None
        await self._record(
            CIRCUIT_CLOSED,
            call,
            {
                "agent": call.agent,
                "probe": True,
                "consecutive_failures": ledger.failures_before_success,
            },
            timestamp=self.clock(),
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

    async def _record(
        self,
        event_type: str,
        call: _Call,
        payload: dict,
        *,
        timestamp: datetime | None = None,
    ) -> None:
        logger.warning("%s: agent=%s objective=%s", event_type, call.agent, call.objective_id)
        if self.sessions is None:
            return
        key = f"{event_type}/{call.objective_id}/{call.agent}"
        if event_type in (CIRCUIT_OPENED, CIRCUIT_CLOSED):
            # The breaker can flip many times; every transition is its own event.
            key = f"{key}/{uuid4().hex}"
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
                    idempotency_key=key,
                    payload=payload,
                    **({"timestamp": timestamp} if timestamp is not None else {}),
                ),
            )
        except Exception as error:  # noqa: BLE001 - audit failure must not re-enable the LLM
            logger.error("could not record %s: %s", event_type, type(error).__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
