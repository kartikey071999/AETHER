"""Model provider usage and cost accounting."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, ProviderUsageRecord
from aether.storage.repositories._events import add_event


class ProviderUsageRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        *,
        provider: str,
        model: str,
        agent: str | None = None,
        task_id: UUID | None = None,
        request_count: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: Decimal | float | str = Decimal(0),
        latency_ms: int | None = None,
        status: str = "succeeded",
        correlation_id: UUID | None = None,
    ) -> UUID:
        cost = Decimal(str(cost_usd))
        if min(request_count, input_tokens, output_tokens) < 0 or cost < 0:
            raise ValueError("Provider usage counts and cost must not be negative")
        async with self._sessions() as session, session.begin():
            usage = ProviderUsageRecord(
                provider=provider,
                model=model,
                agent=agent,
                task_id=task_id,
                request_count=request_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                status=status,
            )
            session.add(usage)
            await session.flush()
            add_event(
                session,
                event_type="provider.usage_recorded",
                aggregate_type="provider_usage",
                aggregate_id=usage.id,
                actor=agent or "SYSTEM",
                payload={
                    "provider": provider,
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_usd": cost,
                    "status": status,
                },
                category=EventCategory.PROVIDER,
                correlation_id=correlation_id,
                task_id=task_id,
            )
            return usage.id

    async def total_cost(
        self,
        *,
        provider: str | None = None,
        since: datetime | None = None,
    ) -> Decimal:
        statement = select(func.coalesce(func.sum(ProviderUsageRecord.cost_usd), 0))
        if provider is not None:
            statement = statement.where(ProviderUsageRecord.provider == provider)
        if since is not None:
            statement = statement.where(ProviderUsageRecord.occurred_at >= since)
        async with self._sessions() as session:
            total = await session.scalar(statement)
        return Decimal(str(total or 0))
