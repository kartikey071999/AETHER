"""Bounded repository reads for the HTTP API, with no fabricated operational state."""

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.models.settings import ProviderSettings
from aether.storage.models import (
    AgentHeartbeatRecord,
    AgentRecord,
    AuthorizationRequestRecord,
    AuthorizationStatus,
    Base,
    CapabilityRecord,
    ErrorRecord,
    EventRecord,
    ExecutionRunRecord,
    MessageRecord,
    ObjectiveRecord,
    RecoveryAttemptRecord,
    RequirementRecord,
    ResourceMeasurementRecord,
    SecurityDecisionRecord,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
    TestResultRecord,
    TestRunRecord,
)
from aether.storage.repositories.provider_usage import ProviderUsageRepository


def record_data(record: Base) -> dict[str, Any]:
    """Only mapped columns, never lazy relationships or SQLAlchemy internals."""
    return {column.key: getattr(record, column.key) for column in record.__table__.columns}


def task_events(task_id: UUID) -> Select[tuple[EventRecord]]:
    return (
        select(EventRecord)
        .where(
            or_(
                EventRecord.task_id == task_id,
                (EventRecord.aggregate_type == "task") & (EventRecord.aggregate_id == task_id),
            )
        )
        .order_by(EventRecord.created_at.desc(), EventRecord.id.desc())
    )


class ApiRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.clock = clock

    async def page(self, statement: Select[Any], *, limit: int, offset: int) -> dict[str, Any]:
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            rows = await session.scalars(statement.limit(limit).offset(offset))
            return {
                "items": [record_data(row) for row in rows],
                "total": int(total or 0),
                "limit": limit,
                "offset": offset,
            }

    async def get(self, model: type[Base], record_id: UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            record = await session.get(model, record_id)
            if record is None:
                raise LookupError("Record does not exist")
            return record_data(record)

    async def health(self) -> dict[str, Any]:
        # Reading the schema, not just SELECT 1, detects an unmigrated database.
        async with self.sessions() as session:
            await session.execute(select(ObjectiveRecord.id).limit(1))
            latest = await session.scalar(
                select(AgentHeartbeatRecord.observed_at)
                .order_by(AgentHeartbeatRecord.observed_at.desc())
                .limit(1)
            )
        return {
            "status": "ok",
            "database": "reachable",
            "api": "running",
            "latest_heartbeat_at": latest,
        }

    async def agents(self, *, limit: int, offset: int) -> dict[str, Any]:
        page = await self.page(
            select(AgentRecord).order_by(AgentRecord.agent_id), limit=limit, offset=offset
        )
        async with self.sessions() as session:
            for agent in page["items"]:
                latest = await session.scalar(
                    select(AgentHeartbeatRecord)
                    .where(AgentHeartbeatRecord.agent_id == agent["agent_id"])
                    .order_by(
                        AgentHeartbeatRecord.observed_at.desc(),
                        AgentHeartbeatRecord.id.desc(),
                    )
                    .limit(1)
                )
                agent["status"] = latest.status if latest is not None else None
                agent["latest_heartbeat"] = record_data(latest) if latest is not None else None
        return page

    async def _progress(self, objectives: list[dict[str, Any]]) -> None:
        if not objectives:
            return
        async with self.sessions() as session:
            counts = await session.execute(
                select(TaskRecord.objective_id, TaskRecord.state, func.count())
                .where(TaskRecord.objective_id.in_([item["id"] for item in objectives]))
                .group_by(TaskRecord.objective_id, TaskRecord.state)
            )
            grouped: dict[UUID, dict[str, int]] = {}
            for objective_id, state, count in counts:
                grouped.setdefault(objective_id, {})[state] = count
        for objective in objectives:
            states = grouped.get(objective["id"], {})
            total = sum(states.values())
            completed = states.get(TaskStatus.COMPLETED.value, 0)
            objective["progress"] = {
                "tasks_by_state": states,
                "total_tasks": total,
                "completed_tasks": completed,
                # A taskless objective has unknown progress, not implicit completion.
                "completion_percent": 100 * completed / total if total else None,
            }

    async def objectives(self, *, limit: int, offset: int) -> dict[str, Any]:
        page = await self.page(
            select(ObjectiveRecord).order_by(ObjectiveRecord.created_at.desc(), ObjectiveRecord.id),
            limit=limit,
            offset=offset,
        )
        await self._progress(page["items"])
        return page

    async def objective(self, objective_id: UUID) -> dict[str, Any]:
        item = await self.get(ObjectiveRecord, objective_id)
        await self._progress([item])
        return item

    async def tasks(
        self, *, state: TaskStatus | None, agent: str | None, limit: int, offset: int
    ) -> dict[str, Any]:
        statement = select(TaskRecord)
        if state is not None:
            statement = statement.where(TaskRecord.state == state.value)
        if agent is not None:
            statement = statement.where(TaskRecord.assigned_agent == agent)
        return await self.page(
            statement.order_by(TaskRecord.priority.desc(), TaskRecord.created_at, TaskRecord.id),
            limit=limit,
            offset=offset,
        )

    async def task(self, task_id: UUID, *, limit: int, offset: int) -> dict[str, Any]:
        item = await self.get(TaskRecord, task_id)
        item["dependencies"] = await self.page(
            select(TaskDependencyRecord)
            .where(TaskDependencyRecord.task_id == task_id)
            .order_by(TaskDependencyRecord.depends_on_task_id),
            limit=limit,
            offset=offset,
        )
        item["lease"] = {
            "owner": item["lease_owner"],
            "expires_at": item["lease_expires_at"],
        }
        item["attempts"] = {"current": item["attempt"], "maximum": item["max_attempts"]}
        item["executions"] = await self.page(
            select(ExecutionRunRecord)
            .where(ExecutionRunRecord.task_id == task_id)
            .order_by(ExecutionRunRecord.started_at.desc(), ExecutionRunRecord.id),
            limit=limit,
            offset=offset,
        )
        item["events"] = await self.page(task_events(task_id), limit=limit, offset=offset)
        return item

    async def capabilities(self, *, limit: int, offset: int) -> dict[str, Any]:
        page = await self.page(
            select(CapabilityRecord).order_by(CapabilityRecord.name, CapabilityRecord.id),
            limit=limit,
            offset=offset,
        )
        for item in page["items"]:
            item["verification_evidence"] = await self.tests(
                capability_id=item["id"], limit=limit, offset=0
            )
        return page

    async def tests(
        self, *, limit: int, offset: int, capability_id: UUID | None = None
    ) -> dict[str, Any]:
        statement = select(TestRunRecord)
        if capability_id is not None:
            statement = statement.where(TestRunRecord.capability_id == capability_id)
        page = await self.page(
            statement.order_by(TestRunRecord.started_at.desc(), TestRunRecord.id),
            limit=limit,
            offset=offset,
        )
        for item in page["items"]:
            item["results"] = await self.page(
                select(TestResultRecord)
                .where(TestResultRecord.test_run_id == item["id"])
                .order_by(TestResultRecord.name, TestResultRecord.id),
                limit=limit,
                offset=0,
            )
        return page

    async def errors(self, *, limit: int, offset: int) -> dict[str, Any]:
        page = await self.page(
            select(ErrorRecord).order_by(ErrorRecord.occurred_at.desc(), ErrorRecord.id),
            limit=limit,
            offset=offset,
        )
        for item in page["items"]:
            item["recovery_attempts"] = await self.page(
                select(RecoveryAttemptRecord)
                .where(RecoveryAttemptRecord.error_id == item["id"])
                .order_by(RecoveryAttemptRecord.attempted_at.desc(), RecoveryAttemptRecord.id),
                limit=limit,
                offset=0,
            )
        return page

    async def pending_approvals(self, *, limit: int, offset: int) -> dict[str, Any]:
        return await self.page(
            select(AuthorizationRequestRecord)
            .where(
                AuthorizationRequestRecord.status == AuthorizationStatus.PENDING.value,
                or_(
                    AuthorizationRequestRecord.expires_at.is_(None),
                    AuthorizationRequestRecord.expires_at > self.clock(),
                ),
            )
            .order_by(AuthorizationRequestRecord.requested_at, AuthorizationRequestRecord.id),
            limit=limit,
            offset=offset,
        )

    async def events(
        self, *, event_type: str | None, task_id: UUID | None, limit: int, offset: int
    ) -> dict[str, Any]:
        statement = task_events(task_id) if task_id is not None else select(EventRecord)
        if event_type is not None:
            statement = statement.where(EventRecord.event_type == event_type)
        return await self.page(
            statement.order_by(None).order_by(EventRecord.created_at.desc(), EventRecord.id.desc()),
            limit=limit,
            offset=offset,
        )

    async def messages(
        self, *, correlation_id: UUID | None, limit: int, offset: int
    ) -> dict[str, Any]:
        statement = select(MessageRecord)
        if correlation_id is not None:
            statement = statement.where(MessageRecord.correlation_id == correlation_id)
        return await self.page(
            statement.order_by(MessageRecord.timestamp.desc(), MessageRecord.id),
            limit=limit,
            offset=offset,
        )

    async def provider_usage(
        self, settings: ProviderSettings, *, limit: int, offset: int
    ) -> dict[str, Any]:
        today = self.clock().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        until = today + timedelta(days=1)
        # Include historical providers even if their current budget was removed.
        from aether.storage.models import ProviderUsageRecord

        async with self.sessions() as session:
            observed = set(await session.scalars(select(ProviderUsageRecord.provider).distinct()))
            names = sorted(observed | settings.budgets.keys())
            items = []
            for name in names[offset : offset + limit]:
                usage = await ProviderUsageRepository.totals_in(
                    session, provider=name, since=today, until=until
                )
                models = []
                for key, policy in sorted(settings.model_budgets.items()):
                    provider, _, model = key.partition("/")
                    if provider == name:
                        totals = await ProviderUsageRepository.totals_in(
                            session, provider=name, model=model, since=today, until=until
                        )
                        models.append(
                            {"model": model, "today": asdict(totals), "budget": policy.model_dump()}
                        )
                policy = settings.budgets.get(name)
                items.append(
                    {
                        "provider": name,
                        "date": today.date().isoformat(),
                        "today": asdict(usage),
                        "budget": policy.model_dump() if policy is not None else None,
                        "model_budgets": models,
                    }
                )
        return {"items": items, "total": len(names), "limit": limit, "offset": offset}


READ_COLLECTIONS = {
    "/requirements": select(RequirementRecord).order_by(
        RequirementRecord.priority.desc(), RequirementRecord.created_at, RequirementRecord.id
    ),
    "/security/decisions": select(SecurityDecisionRecord).order_by(
        SecurityDecisionRecord.created_at.desc(), SecurityDecisionRecord.id
    ),
    "/scheduler/decisions": select(EventRecord)
    .where(EventRecord.event_type == "scheduler.decision")
    .order_by(EventRecord.created_at.desc(), EventRecord.id.desc()),
    "/resources": select(ResourceMeasurementRecord).order_by(
        ResourceMeasurementRecord.observed_at.desc(), ResourceMeasurementRecord.id
    ),
    "/executions": select(ExecutionRunRecord).order_by(
        ExecutionRunRecord.started_at.desc(), ExecutionRunRecord.id
    ),
}
