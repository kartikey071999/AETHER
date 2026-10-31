"""Per-call environment for ORCHESTR workflows, built from the injected ``AgentContext``."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from aether.agents.base import AgentContext
from aether.agents.orchestr.communication import UpdateEvent, build_handoff, should_notify_user
from aether.agents.orchestr.limits import LIMITS_SERVICE, OrchestrLimits
from aether.agents.orchestr.store import OrchestrStore, stable_id
from aether.communication.contracts import build_message
from aether.communication.messages import AgentId, AgentMessage
from aether.runtime.resource_monitor import HostResources, ResourceMonitor
from aether.runtime.scheduler import Scheduler, SchedulerPolicy
from aether.storage.models import ObjectiveRecord


class Monitor(Protocol):
    def snapshot(self, disk_path: Path) -> HostResources: ...


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(slots=True)
class TickState:
    """In-memory dedup for scheduler-decision events; losing it only repeats one event."""

    last_signature: str | None = None


@dataclass(slots=True)
class Env:
    ctx: AgentContext
    store: OrchestrStore
    limits: OrchestrLimits
    scheduler: Scheduler
    monitor: Monitor
    disk_path: Path
    state: TickState = field(default_factory=TickState)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("aether.orchestr"))

    def now(self) -> datetime:
        return as_utc(self.ctx.clock())

    async def publish(self, message: AgentMessage) -> bool:
        return await self.ctx.bus.publish(message)

    async def send_user_update(
        self,
        event: UpdateEvent,
        *,
        key: str,
        what_happened: str,
        objective: ObjectiveRecord | None = None,
        task_id: UUID | None = None,
        results: list[dict] | None = None,
        verified: list[str] | None = None,
        uncertain: list[str] | None = None,
        blockers: list[str] | None = None,
        decisions_required: list[str] | None = None,
        authorization_request_id: UUID | None = None,
    ) -> bool:
        """Hand one structured update to MUSE if the policy says the user needs it.

        ``key`` identifies the update: it is both the bus message id and a persisted marker, so a
        repeated trigger (redelivery, restart, a second code path) sends nothing new.
        """
        if not should_notify_user(event, self.limits.deadline_escalation_hours):
            return False
        marker = f"orchestr:notify:{key}"
        if await self.store.get_decision(marker) is not None:
            return False
        objective_id = objective.id if objective is not None else None
        payload = build_handoff(
            event,
            what_happened=what_happened,
            objective_id=objective_id,
            task_id=task_id,
            results=results or [],
            verified=verified or [],
            uncertain=uncertain or [],
            blockers=blockers or [],
            decisions_required=decisions_required or [],
            authorization_request_id=authorization_request_id,
        )
        message = build_message(
            AgentId.ORCHESTR,
            AgentId.MUSE,
            payload,
            message_id=stable_id("notify", key),
            objective_id=objective_id,
            task_id=task_id,
            priority=objective.priority if objective is not None else 50,
            requires_response=bool(decisions_required) or authorization_request_id is not None,
        )
        await self.publish(message)
        await self.store.record_decision(
            "user_update",
            f"{event.kind.value} update handed to MUSE",
            key=marker,
            evidence=[event.kind.value, str(payload.urgency.value)],
            objective_id=objective_id,
            task_id=task_id,
        )
        return True


def build_env(ctx: AgentContext, state: TickState | None = None) -> Env:
    services = ctx.services
    limits = services.get(LIMITS_SERVICE)
    policy = services.get("scheduler_policy")
    return Env(
        ctx=ctx,
        store=OrchestrStore(ctx.sessions),
        limits=limits if isinstance(limits, OrchestrLimits) else OrchestrLimits(),
        scheduler=Scheduler(
            policy if isinstance(policy, SchedulerPolicy) else SchedulerPolicy(), ctx.sessions
        ),
        monitor=services.get("resource_monitor") or ResourceMonitor(),
        disk_path=Path(services.get("disk_path", ".")),
        state=state or TickState(),
    )
