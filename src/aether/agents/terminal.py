"""Terminal-objective guard: work-creating messages for a finished objective are only recorded.

Messages queued for an objective that was cancelled, completed or failed (for example blockers of
a cancelled runaway objective) must never escalate, replan, create work or reach the user. The
receiving agent acknowledges them and records one ``ignored_terminal_objective`` decision.
"""

from typing import Any
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.work_queue import task_ref_in
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.storage.models import ObjectiveRecord, RequirementRecord

TERMINAL_OBJECTIVE_STATES = frozenset({"cancelled", "completed", "failed"})
IGNORED_DECISION = "ignored_terminal_objective"

# Message types that make a worker agent start, plan or continue work for an objective.
WORK_MESSAGE_TYPES = frozenset(
    {
        MessageType.TASK_ASSIGN,
        MessageType.RESEARCH_REQUEST,
        MessageType.RESEARCH_FINDING,
        MessageType.RESEARCH_CONSOLIDATED,
        MessageType.REQUIREMENT_SUBMIT,
        MessageType.REQUIREMENT_EVALUATED,
        MessageType.SECURITY_EVALUATE,
        MessageType.IMPLEMENTATION_PLAN,
        MessageType.EXECUTION_REQUEST,
        MessageType.VERIFICATION_REQUEST,
        MessageType.TEST_REPORT,
        MessageType.BLOCKER,
    }
)

# MUSE carries user communication (e.g. the final notice of a just-completed objective and its
# outbox review); the user's channel is never dropped by this guard.
_EXEMPT_SOURCES = frozenset({AgentId.MUSE})


def _uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


async def objective_of(session: AsyncSession, message: AgentMessage) -> ObjectiveRecord | None:
    """The objective a message belongs to: envelope, then payload, then its task/requirement."""
    payload = message.payload if isinstance(message.payload, dict) else {}
    objective_id = message.objective_id or _uuid(payload.get("objective_id"))
    if objective_id is None:
        task_id = message.task_id or _uuid(payload.get("task_id"))
        task = await task_ref_in(session, task_id) if task_id else None
        objective_id = task.objective_id if task is not None else None
    if objective_id is None:
        requirement_id = message.requirement_id or _uuid(payload.get("requirement_id"))
        requirement = (
            await session.get(RequirementRecord, requirement_id) if requirement_id else None
        )
        objective_id = requirement.objective_id if requirement is not None else None
    return await session.get(ObjectiveRecord, objective_id) if objective_id else None


async def terminal_objective(
    sessions: async_sessionmaker[AsyncSession], message: AgentMessage
) -> ObjectiveRecord | None:
    """The message's objective when it is in a terminal state, else ``None``."""
    async with sessions() as session:
        objective = await objective_of(session, message)
    if objective is None or str(objective.status) not in TERMINAL_OBJECTIVE_STATES:
        return None
    return objective


def ignored_key(agent: AgentId, message: AgentMessage) -> str:
    return f"{agent.value.lower()}:ignored-terminal:{message.message_id}"


async def record_ignored(
    sessions: async_sessionmaker[AsyncSession],
    agent: AgentId,
    message: AgentMessage,
    objective: ObjectiveRecord,
) -> None:
    """Record (once per message) that ``agent`` acknowledged and ignored ``message``."""
    key = ignored_key(agent, message)
    event = build_event(
        "decision.recorded",
        source=f"agents.{agent.value.lower()}",
        actor=agent.value,
        aggregate_type="objective",
        aggregate_id=objective.id,
        objective_id=objective.id,
        task_id=message.task_id,
        correlation_id=message.correlation_id,
        idempotency_key=key,
        event_id=uuid5(message.message_id, key),
        payload={
            "decision": IGNORED_DECISION,
            "rationale": f"objective is {objective.status}; "
            f"{message.message_type.value} from {message.source_agent.value} acknowledged only",
            "evidence": [message.message_type.value, message.source_agent.value, objective.status],
        },
    )
    await EventRecorder().record(sessions, event)


async def ignore_if_terminal(
    agent: AgentId,
    message: AgentMessage,
    sessions: async_sessionmaker[AsyncSession],
    *,
    message_types: frozenset[MessageType] = WORK_MESSAGE_TYPES,
) -> bool:
    """True (after recording the decision) when ``message`` targets a finished objective."""
    if message.message_type not in message_types or message.source_agent in _EXEMPT_SOURCES:
        return False
    objective = await terminal_objective(sessions, message)
    if objective is None:
        return False
    await record_ignored(sessions, agent, message, objective)
    return True


__all__ = [
    "IGNORED_DECISION",
    "TERMINAL_OBJECTIVE_STATES",
    "WORK_MESSAGE_TYPES",
    "ignore_if_terminal",
    "objective_of",
    "record_ignored",
    "terminal_objective",
]
