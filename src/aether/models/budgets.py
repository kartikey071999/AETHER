"""Durable, conservative reservations protect daily and concurrent quotas."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from uuid import UUID

from aether.models.contracts import CompletionRequest, CompletionResponse
from aether.models.costs import estimate_cost
from aether.models.settings import BudgetPolicy, ProviderSettings
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.security import OperationBlocked
from aether.storage.repositories.provider_usage import ProviderUsageRepository, UsageTotals


class BudgetExceeded(RuntimeError):
    pass


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

    async def reserve(self, provider: str, request: CompletionRequest) -> Reservation:
        if request.model is None:
            raise ValueError("budget admission requires a selected model")
        policy = self.settings.budgets.get(provider)
        if policy is None:
            raise BudgetExceeded(f"{provider}: no budget policy configured")
        input_tokens = request.input_token_estimate()
        tokens = input_tokens + request.max_tokens
        cost, unknown = estimate_cost(provider, request.model, input_tokens, request.max_tokens)
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
            model_policy = self.settings.model_budgets.get(f"{provider}/{request.model}")
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
    ) -> None:
        """Finalize usage and both events atomically; write errors propagate, never fall back."""
        blocked = isinstance(error, OperationBlocked)
        estimated = response is None or response.usage_estimated
        input_tokens = (
            response.input_tokens if response else (0 if blocked else reservation.input_tokens)
        )
        output_tokens = (
            response.output_tokens if response else (0 if blocked else reservation.output_tokens)
        )
        cost, unknown = estimate_cost(
            reservation.provider, reservation.model, input_tokens, output_tokens
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
