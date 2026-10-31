"""Startup recovery: stale leases and in-flight messages are rebuilt from the database."""

from collections.abc import Collection
from dataclasses import dataclass, field

from aether.communication.bus import MessageBus
from aether.communication.messages import AgentId
from aether.runtime.leases import LeaseManager, RecoveryOutcome


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    recovered_tasks: list[RecoveryOutcome] = field(default_factory=list)
    requeued_messages: int = 0
    pending_messages: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "recovered_tasks": len(self.recovered_tasks),
            "failed_tasks": sum(1 for item in self.recovered_tasks if item.new_status == "failed"),
            "requeued_messages": self.requeued_messages,
            "pending_messages": self.pending_messages,
        }


async def recover_runtime_state(
    leases: LeaseManager,
    bus: MessageBus,
    agent_ids: Collection[AgentId],
) -> RecoveryReport:
    """Recover work left behind by a previous process.

    Only messages whose claim has expired (or that predate claims) are returned to pending, so a
    second engine starting against the same database leaves a live engine's in-flight messages
    alone. A crashed engine's messages come back once its claim expires; a cleanly stopped one
    releases them at shutdown.
    """
    recovered = await leases.recover_expired()
    requeued = await bus.requeue_in_flight(agent_ids)
    pending = await bus.agents_with_pending()
    for agent_id in agent_ids:
        if pending.get(agent_id.value):
            bus.wake(agent_id)
    return RecoveryReport(
        recovered_tasks=recovered,
        requeued_messages=requeued,
        pending_messages=pending,
    )
