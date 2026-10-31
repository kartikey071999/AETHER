"""Model provider usage and cost accounting."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import EventCategory, ProviderUsageRecord
from aether.storage.repositories._events import add_event


@dataclass(frozen=True)
class UsageTotals:
    requests: int
    tokens: int
    cost_usd: Decimal
    active: int


class ProviderUsageRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @asynccontextmanager
    async def locked(self, provider: str) -> AsyncIterator[AsyncSession]:
        """Serialize quota admission across processes, not just a single router."""
        async with self._sessions() as session, session.begin():
            dialect = session.get_bind().dialect.name
            if dialect == "postgresql":
                key = int.from_bytes(
                    hashlib.sha256(f"aether.provider.{provider}".encode()).digest()[:8],
                    signed=True,
                )
                await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            elif dialect == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            else:
                raise RuntimeError("provider budget locking requires PostgreSQL or SQLite")
            yield session

    @staticmethod
    async def totals_in(
        session: AsyncSession,
        *,
        provider: str,
        since: datetime,
        until: datetime,
        model: str | None = None,
    ) -> UsageTotals:
        filters = [ProviderUsageRecord.provider == provider]
        if model is not None:
            filters.append(ProviderUsageRecord.model == model)
        statement = select(
            func.coalesce(func.sum(ProviderUsageRecord.request_count), 0),
            func.coalesce(
                func.sum(ProviderUsageRecord.input_tokens + ProviderUsageRecord.output_tokens), 0
            ),
            func.coalesce(func.sum(ProviderUsageRecord.cost_usd), 0),
        ).where(
            *filters,
            ProviderUsageRecord.occurred_at >= since,
            ProviderUsageRecord.occurred_at < until,
        )
        requests, tokens, cost = (await session.execute(statement)).one()
        # A pre-midnight or interrupted reservation remains active until explicitly finalized.
        active = await session.scalar(
            select(func.count())
            .select_from(ProviderUsageRecord)
            .where(*filters, ProviderUsageRecord.status == "reserved")
        )
        return UsageTotals(int(requests), int(tokens), Decimal(str(cost)), int(active or 0))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            yield session

    @staticmethod
    def stage_reservation(
        session: AsyncSession,
        *,
        provider: str,
        model: str,
        agent: str,
        task_id: UUID | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
        occurred_at: datetime,
    ) -> UUID:
        reservation_id = uuid4()
        session.add(
            ProviderUsageRecord(
                id=reservation_id,
                provider=provider,
                model=model,
                agent=agent,
                task_id=task_id,
                request_count=1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                status="reserved",
                occurred_at=occurred_at,
            )
        )
        return reservation_id

    @staticmethod
    async def finalize_in(
        session: AsyncSession,
        reservation_id: UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Decimal,
        status: str,
        latency_ms: int,
    ) -> None:
        usage = await session.scalar(
            select(ProviderUsageRecord)
            .where(ProviderUsageRecord.id == reservation_id)
            .with_for_update()
        )
        if usage is None or usage.status != "reserved":
            raise RuntimeError("provider reservation is missing or already finalized")
        usage.input_tokens = input_tokens
        usage.output_tokens = output_tokens
        usage.cost_usd = cost_usd
        usage.status = status
        usage.latency_ms = latency_ms

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
