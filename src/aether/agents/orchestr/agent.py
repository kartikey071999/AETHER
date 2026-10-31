"""The ORCHESTR agent: central coordination of objectives, tasks, priorities and blockers."""

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.orchestr.env import TickState, build_env
from aether.agents.orchestr.scheduling import TickReport, tick
from aether.agents.orchestr.workflow import dispatch
from aether.communication.contracts import receivable_types
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import TaskRecord

COORDINATION_KEY_PREFIX = "coordination"


class OrchestrAgent(BaseAgent):
    """Owns planning and scheduling; all durable state lives in the database, none in memory.

    The only in-memory state is a dedup hint for scheduler-decision events, so a restarted
    instance resumes exactly where the database says the system is.
    """

    agent_id = AgentId.ORCHESTR
    accepted_message_types = receivable_types(AgentId.ORCHESTR)

    def __init__(self) -> None:
        super().__init__()
        self._state = TickState()

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        env = build_env(ctx, self._state)
        await dispatch(env, message)
        try:
            await tick(env)
        except Exception:
            env.logger.exception("orchestr tick failed after %s", message.message_type.value)

    async def tick(self, ctx: AgentContext) -> TickReport:
        """Run one scheduling pass; callable on a timer or at startup by the runtime."""
        return await tick(build_env(ctx, self._state))

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        if not (task.idempotency_key or "").startswith(COORDINATION_KEY_PREFIX):
            return TaskResult(
                status=TaskResultStatus.FAILED,
                summary="ORCHESTR coordinates work and does not execute worker tasks",
                error="task is not an ORCHESTR coordination task",
                retryable=False,
            )
        report = await self.tick(ctx)
        return TaskResult(
            status=TaskResultStatus.COMPLETED,
            summary="coordination pass finished",
            outputs={
                "released": len(report.released),
                "completed_objectives": len(report.completed),
            },
        )
