"""FORGE: turns an approved plan into recorded, reversible work and never self-certifies it."""

import asyncio
import logging
from typing import ClassVar

from aether.agents.base import AgentContext, BaseAgent, TaskResult, TaskResultStatus
from aether.agents.forge.outbound import blocker_message
from aether.agents.forge.preflight import Preflight
from aether.agents.forge.resolve import resolve_plan
from aether.agents.forge.schemas import ForgeConfig
from aether.agents.forge.store import plan_from_message
from aether.agents.forge.workflow import ForgeWorkflow, Sleep
from aether.communication.contracts import (
    BlockerKind,
    InvalidPayload,
    TaskAssignPayload,
    TestReportPayload,
    receivable_types,
    validate_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.storage.models import TaskRecord

logger = logging.getLogger("aether.agent.forge")


class ForgeAgent(BaseAgent):
    agent_id: ClassVar[AgentId] = AgentId.FORGE
    guard_terminal_objectives = True
    accepted_message_types = receivable_types(AgentId.FORGE)

    def __init__(self, config: ForgeConfig | None = None, sleep: Sleep = asyncio.sleep) -> None:
        super().__init__()
        self._config = config or ForgeConfig()
        self._sleep = sleep

    async def execute_task(self, task: TaskRecord, ctx: AgentContext) -> TaskResult:
        try:
            return await ForgeWorkflow(self, self._config, self._sleep).run(task, ctx)
        except Exception as error:
            logger.exception("forge task %s crashed", task.id)
            return TaskResult(
                status=TaskResultStatus.FAILED,
                summary="FORGE hit an unexpected internal error",
                error=f"{type(error).__name__}: {error}"[:500],
                retryable=True,
            )

    async def handle_message(self, message: AgentMessage, ctx: AgentContext) -> None:
        try:
            payload = validate_message(message)
        except InvalidPayload as error:
            await self._report(message, ctx, f"{message.message_type.value} was invalid: {error}")
            raise
        kind = message.message_type
        if kind is MessageType.TASK_ASSIGN:
            assert isinstance(payload, TaskAssignPayload)
            await self._on_assign(message, payload, ctx)
        elif kind in (MessageType.EXECUTION_REQUEST, MessageType.IMPLEMENTATION_PLAN):
            await self._on_plan(message, ctx)
        elif kind is MessageType.TEST_REPORT:
            assert isinstance(payload, TestReportPayload)
            await self._on_test_report(message, payload, ctx)
        else:
            logger.info("forge acknowledged %s", kind.value)

    async def _task(self, message: AgentMessage, ctx: AgentContext) -> TaskRecord | None:
        if message.task_id is None:
            return None
        async with ctx.sessions() as session:
            return await session.get(TaskRecord, message.task_id)

    async def _report(
        self,
        message: AgentMessage,
        ctx: AgentContext,
        description: str,
        *,
        suffix: str = "invalid",
    ) -> None:
        task = await self._task(message, ctx)
        if task is None:
            return
        await ctx.bus.publish(
            blocker_message(
                task,
                kind=BlockerKind.IMPLEMENTATION,
                route_to=AgentId.ORCHESTR,
                description=description,
                key=f"message:{message.message_id}:{suffix}",
            )
        )

    async def _on_assign(
        self, message: AgentMessage, payload: TaskAssignPayload, ctx: AgentContext
    ) -> None:
        task = await self._task(message, ctx)
        if task is None:
            raise LookupError(f"task {payload.task_id} does not exist")
        plan, refusal = await resolve_plan(
            ctx, task, self._config, criteria_hint=payload.acceptance_criteria
        )
        if plan is None and refusal is not None:
            await self._report(
                message,
                ctx,
                f"FORGE cannot plan the assigned task: {refusal.code.value}: {refusal.detail}",
                suffix="assign",
            )

    async def _on_plan(self, message: AgentMessage, ctx: AgentContext) -> None:
        task = await self._task(message, ctx)
        plan = plan_from_message(message)
        if task is None or plan is None:
            return
        report = await Preflight(self._config).check_plan_shape(task, plan, ctx)
        if report.refusals:
            detail = "; ".join(f"{item.code.value}: {item.detail}" for item in report.refusals)
            await self._report(
                message, ctx, f"the received plan is not executable: {detail}", suffix="plan"
            )

    async def _on_test_report(
        self, message: AgentMessage, payload: TestReportPayload, ctx: AgentContext
    ) -> None:
        if payload.verdict.value == "verified":
            return
        await self._report(
            message,
            ctx,
            f"verification of FORGE work ended with verdict {payload.verdict.value}; "
            f"{payload.results.failed} failed, {payload.results.errors} errored: {payload.summary}",
            suffix="test-report",
        )


AGENT_CLASS = ForgeAgent
