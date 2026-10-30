"""Agent configuration records and health heartbeats."""

from datetime import timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.storage.models import (
    AgentHealthStatus,
    AgentHeartbeatRecord,
    AgentRecord,
    EventCategory,
    utcnow,
)
from aether.storage.repositories._events import add_event, as_utc


def agent_aggregate_id(agent_id: str) -> UUID:
    """Stable UUID used as the event aggregate id for a string-keyed agent."""
    return uuid5(NAMESPACE_URL, f"aether:agent:{agent_id}")


class AgentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert(
        self,
        agent_id: str,
        *,
        responsibility: str,
        actor: str = "ORCHESTR",
        enabled: bool | None = None,
        model_preference: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> bool:
        """Create or update an agent row; returns True when a new row was created."""
        agent_id = str(agent_id)
        async with self._sessions() as session, session.begin():
            agent = await session.get(AgentRecord, agent_id, with_for_update=True)
            created = agent is None
            if agent is None:
                agent = AgentRecord(agent_id=agent_id, responsibility=responsibility)
                session.add(agent)
            agent.responsibility = responsibility
            if enabled is not None:
                agent.enabled = enabled
            if model_preference is not None:
                agent.model_preference = model_preference
            if config is not None:
                agent.config = config
            agent.updated_at = utcnow()
            add_event(
                session,
                event_type="agent.registered" if created else "agent.updated",
                aggregate_type="agent",
                aggregate_id=agent_aggregate_id(agent_id),
                actor=actor,
                payload={
                    "agent_id": agent_id,
                    "enabled": agent.enabled,
                    "model_preference": agent.model_preference,
                },
                category=EventCategory.AGENT,
            )
            return created

    async def set_enabled(self, agent_id: str, enabled: bool, *, actor: str) -> None:
        agent_id = str(agent_id)
        async with self._sessions() as session, session.begin():
            agent = await session.get(AgentRecord, agent_id, with_for_update=True)
            if agent is None:
                raise LookupError(f"Agent {agent_id} does not exist")
            agent.enabled = enabled
            agent.updated_at = utcnow()
            add_event(
                session,
                event_type="agent.enabled" if enabled else "agent.disabled",
                aggregate_type="agent",
                aggregate_id=agent_aggregate_id(agent_id),
                actor=actor,
                payload={"agent_id": agent_id},
                category=EventCategory.AGENT,
            )

    async def get(self, agent_id: str) -> AgentRecord | None:
        async with self._sessions() as session:
            return await session.get(AgentRecord, str(agent_id))


class AgentHeartbeatRepository:
    """Heartbeats are audited; status changes use a distinct ``agent.status_changed`` event."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        agent_id: str,
        status: AgentHealthStatus,
        *,
        current_task_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        agent_id = str(agent_id)
        status = AgentHealthStatus(status)
        async with self._sessions() as session, session.begin():
            previous = await _latest_heartbeat(session, agent_id)
            observed_at = utcnow()
            previous_observed_at = as_utc(previous.observed_at) if previous is not None else None
            # Keep per-agent ordering strict even when the system clock is coarse.
            if previous_observed_at is not None and observed_at <= previous_observed_at:
                observed_at = previous_observed_at + timedelta(microseconds=1)
            heartbeat = AgentHeartbeatRecord(
                agent_id=agent_id,
                status=status,
                current_task_id=current_task_id,
                details=details or {},
                observed_at=observed_at,
            )
            session.add(heartbeat)
            await session.flush()
            previous_status = previous.status if previous is not None else None
            changed = previous_status != status
            add_event(
                session,
                event_type="agent.status_changed" if changed else "agent.heartbeat",
                aggregate_type="agent",
                aggregate_id=agent_aggregate_id(agent_id),
                actor=agent_id,
                payload={
                    "agent_id": agent_id,
                    "from": previous_status,
                    "to": status,
                    "heartbeat_id": heartbeat.id,
                    "current_task_id": current_task_id,
                },
                category=EventCategory.AGENT,
                correlation_id=correlation_id,
                task_id=current_task_id,
            )
            return heartbeat.id

    async def latest(self, agent_id: str) -> AgentHeartbeatRecord | None:
        async with self._sessions() as session:
            return await _latest_heartbeat(session, str(agent_id))


async def _latest_heartbeat(session: AsyncSession, agent_id: str) -> AgentHeartbeatRecord | None:
    ordering = (AgentHeartbeatRecord.observed_at.desc(), AgentHeartbeatRecord.id.desc())
    statement = select(AgentHeartbeatRecord).where(AgentHeartbeatRecord.agent_id == agent_id)
    return await session.scalar(statement.order_by(*ordering).limit(1))
