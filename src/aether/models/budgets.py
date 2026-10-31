"""Durable, conservative reservations protect daily and concurrent quotas."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.models.base import EmptyCompletion
from aether.models.contracts import CompletionRequest, CompletionResponse, ModelInfo
from aether.models.costs import estimate_cost
from aether.models.settings import BudgetPolicy, ProviderSettings
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.security import OperationBlocked
from aether.storage.models import ProviderUsageRecord
from aether.storage.repositories.provider_usage import ProviderUsageRepository, UsageTotals


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class RouteSpend:
    """Provider calls and tokens one routed completion consumed, with optional hard limits.

    The router refuses any provider call (including fallbacks and retries) that could push the
    totals past the limits; ``refusal`` then names the limit that stopped it.
    """

    max_requests: int | None = None
    max_tokens: int | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    refusal: str | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def check(self, request: CompletionRequest) -> None:
        if self.max_requests is not None and self.requests + 1 > self.max_requests:
            self.refusal = "objective request budget"
        elif (
            self.max_tokens is not None
            and self.tokens + request.input_token_estimate() + request.max_tokens > self.max_tokens
        ):
            self.refusal = "objective token budget"
        else:
            return
        raise BudgetExceeded(f"exceeded {self.refusal}")

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.requests += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


@dataclass(frozen=True)
class Reservation:
    id: UUID
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    cost_estimate_unknown: bool


class BudgetGuard:
    def __init__(
        self,
        usage: ProviderUsageRepository,
        settings: ProviderSettings,
        recorder: EventRecorder | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.usage = usage
        self.settings = settings
        self.recorder = recorder or EventRecorder()
        self.clock = clock

    def prepare_request(
        self, provider: str, model: ModelInfo, request: CompletionRequest
    ) -> CompletionRequest:
        policy = self.settings.budgets.get(provider)
        if policy is None:
            raise BudgetExceeded(f"{provider}: no budget policy configured")
        model_policy = self.settings.model_budgets.get(f"{provider}/{model.name}")
        cap = min(
            policy.max_tokens_per_request,
            model_policy.max_tokens_per_request if model_policy else policy.max_tokens_per_request,
        )
        try:
            return request.with_model_limits(model, max_tokens_per_request=cap).model_copy(
                update={"provider": provider}
            )
        except ValueError as error:
            raise BudgetExceeded(f"{provider}: {error}") from None

    async def reserve(self, provider: str, request: CompletionRequest) -> Reservation:
        if request.model is None:
            raise ValueError("budget admission requires a selected model")
        policy = self.settings.budgets.get(provider)
        if policy is None:
            raise BudgetExceeded(f"{provider}: no budget policy configured")
        input_tokens = request.input_token_estimate()
        tokens = input_tokens + request.max_tokens
        if request.max_tokens_cap is not None and request.max_tokens > request.max_tokens_cap:
            raise BudgetExceeded(f"{provider}: exceeded request output token cap")
        cost, unknown = estimate_cost(
            provider,
            request.model,
            input_tokens,
            request.max_tokens,
            prices=self.settings.prices,
        )
        model_policy = self.settings.model_budgets.get(f"{provider}/{request.model}")
        if (
            unknown
            and not policy.allow_unpriced
            and (
                policy.max_cost_usd_per_day is not None
                or (model_policy is not None and model_policy.max_cost_usd_per_day is not None)
            )
        ):
            raise BudgetExceeded(f"{provider}: price unknown while a USD ceiling is configured")
        cost = cost.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        async with self.usage.locked(provider) as session:
            now = self.clock()
            if now.utcoffset() is None:
                raise ValueError("budget clock must include a timezone")
            now = now.astimezone(UTC)
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            totals = await self.usage.totals_in(
                session, provider=provider, since=day, until=day + timedelta(days=1)
            )
            self._check(provider, policy, totals, tokens, cost)
            if model_policy is not None:
                model_totals = await self.usage.totals_in(
                    session,
                    provider=provider,
                    model=request.model,
                    since=day,
                    until=day + timedelta(days=1),
                )
                self._check(provider, model_policy, model_totals, tokens, cost)
            reservation_id = self.usage.stage_reservation(
                session,
                provider=provider,
                model=request.model,
                agent=request.agent.value,
                task_id=request.task_id,
                objective_id=request.objective_id,
                input_tokens=input_tokens,
                output_tokens=request.max_tokens,
                cost_usd=cost,
                occurred_at=now,
            )
            reservation = Reservation(
                reservation_id,
                provider,
                request.model,
                input_tokens,
                request.max_tokens,
                cost,
                unknown,
            )
        return reservation

    @staticmethod
    def _check(
        provider: str, policy: BudgetPolicy, totals: UsageTotals, tokens: int, cost: Decimal
    ) -> None:
        checks = (
            (totals.active >= policy.max_concurrent_requests, "concurrent requests"),
            (tokens > policy.max_tokens_per_request, "tokens per request"),
            (
                policy.max_requests_per_day is not None
                and totals.requests + 1 > policy.max_requests_per_day,
                "daily requests",
            ),
            (
                policy.max_tokens_per_day is not None
                and totals.tokens + tokens > policy.max_tokens_per_day,
                "daily tokens",
            ),
            (
                policy.max_cost_usd_per_day is not None
                and totals.cost_usd + cost > Decimal(str(policy.max_cost_usd_per_day)),
                "daily cost",
            ),
        )
        for exceeded, reason in checks:
            if exceeded:
                raise BudgetExceeded(f"{provider}: exceeded {reason}")

    async def finish(
        self,
        reservation: Reservation,
        request: CompletionRequest,
        *,
        response: CompletionResponse | None,
        error: BaseException | None,
        latency_ms: float,
        attempt: int,
    ) -> tuple[int, int]:
        """Finalize usage and both events atomically; write errors propagate, never fall back.

        Returns the (input, output) tokens charged for the attempt.
        """
        blocked = isinstance(error, OperationBlocked)
        measured = (
            response
            if response is not None
            else (error if isinstance(error, EmptyCompletion) else None)
        )
        estimated = measured is None or measured.usage_estimated
        input_tokens = (
            measured.input_tokens
            if measured is not None
            else (0 if blocked else reservation.input_tokens)
        )
        output_tokens = (
            measured.output_tokens
            if measured is not None
            else (0 if blocked else reservation.output_tokens)
        )
        cost, unknown = estimate_cost(
            reservation.provider,
            reservation.model,
            input_tokens,
            output_tokens,
            prices=self.settings.prices,
        )
        # Storage is Numeric(12, 6); round upward rather than undercount small calls.
        cost = cost.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
        status = (
            "succeeded"
            if error is None
            else (
                "blocked"
                if blocked
                else ("cancelled" if not isinstance(error, Exception) else "failed")
            )
        )
        async with self.usage.transaction() as session:
            await self.usage.finalize_in(
                session,
                reservation.id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                status=status,
                latency_ms=round(latency_ms),
            )
            for event_type, payload in (
                (
                    "provider.usage_recorded",
                    {
                        "provider": reservation.provider,
                        "model": reservation.model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_usd": cost,
                        "status": status,
                    },
                ),
                (
                    "provider.request_completed",
                    {
                        "provider": reservation.provider,
                        "model": reservation.model,
                        "tokens": input_tokens + output_tokens,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost": float(cost),
                        "latency_ms": latency_ms,
                        "status": status,
                        "attempt": attempt,
                        "error_type": type(error).__name__ if error else None,
                        "usage_estimated": estimated,
                        "cost_estimate_unknown": unknown,
                    },
                ),
            ):
                self.recorder.add(
                    session,
                    build_event(
                        event_type,
                        source="aether.models",
                        actor=request.agent.value,
                        aggregate_type="provider_usage",
                        aggregate_id=reservation.id,
                        correlation_id=reservation.id,
                        objective_id=request.objective_id,
                        task_id=request.task_id,
                        payload=payload,
                    ),
                )
        return input_tokens, output_tokens


@dataclass(frozen=True)
class ReconciliationSummary:
    abandoned: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)


async def reconcile_stale_reservations(
    sessions: async_sessionmaker[AsyncSession], *, older_than: timedelta, now: datetime
) -> ReconciliationSummary:
    """Release expired slots; older_than must include the call timeout and safety margin."""
    if older_than <= timedelta(0):
        raise ValueError("reservation age must be positive and include a timeout safety margin")
    if now.utcoffset() is None:
        raise ValueError("reconciliation time must include a timezone")
    now = now.astimezone(UTC)
    cutoff = now - older_than
    filters = (
        ProviderUsageRecord.status == "reserved",
        ProviderUsageRecord.occurred_at < cutoff,
    )
    async with sessions() as session:
        providers = list(
            await session.scalars(select(ProviderUsageRecord.provider).where(*filters).distinct())
        )
    usage = ProviderUsageRepository(sessions)
    recorder = EventRecorder()
    abandoned = input_tokens = output_tokens = 0
    cost = Decimal(0)
    for provider in sorted(providers):
        async with usage.locked(provider) as session:
            rows = await session.scalars(
                select(ProviderUsageRecord)
                .where(*filters, ProviderUsageRecord.provider == provider)
                .with_for_update()
            )
            for row in rows:
                row.status = "abandoned"
                # Charge old interrupted calls to the recovery day, rather than freeing quota.
                row.occurred_at = now
                recorder.add(
                    session,
                    build_event(
                        "provider.usage_recorded",
                        timestamp=now,
                        source="aether.models",
                        actor=row.agent or "SYSTEM",
                        aggregate_type="provider_usage",
                        aggregate_id=row.id,
                        correlation_id=row.id,
                        task_id=row.task_id,
                        idempotency_key=f"provider.reservation_abandoned/{row.id}",
                        payload={
                            "provider": row.provider,
                            "model": row.model,
                            "input_tokens": row.input_tokens,
                            "output_tokens": row.output_tokens,
                            "cost_usd": row.cost_usd,
                            "status": "abandoned",
                        },
                    ),
                )
                abandoned += 1
                input_tokens += row.input_tokens
                output_tokens += row.output_tokens
                cost += row.cost_usd
    return ReconciliationSummary(abandoned, input_tokens, output_tokens, cost)
