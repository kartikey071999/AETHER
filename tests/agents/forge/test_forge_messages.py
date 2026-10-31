from uuid import uuid4

import pytest
from forge_support import ForgeEnv, step, write

from aether.agents.base import UnsupportedMessageType
from aether.agents.factory import build_agents, resolve_agent_class
from aether.agents.forge import AGENT_CLASS, ForgeAgent
from aether.communication.contracts import (
    InvalidPayload,
    TaskAssignPayload,
    TestCounts,
    TestReportPayload,
    VerificationVerdict,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType


def test_factory_discovers_forge_instead_of_a_placeholder() -> None:
    assert AGENT_CLASS is ForgeAgent
    assert resolve_agent_class(AgentId.FORGE) is ForgeAgent
    forge = next(agent for agent in build_agents() if agent.agent_id is AgentId.FORGE)
    assert isinstance(forge, ForgeAgent)
    assert MessageType.EXECUTION_REQUEST in forge.accepted_message_types
    assert MessageType.TASK_ASSIGN in forge.accepted_message_types


async def test_unsupported_message_types_are_rejected(env: ForgeEnv) -> None:
    message = AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.OBJECTIVE_CREATE,
        payload={},
    )
    with pytest.raises(UnsupportedMessageType):
        await env.agent.dispatch_message(message, env.ctx)


async def test_invalid_payload_raises_and_reports_a_blocker(env: ForgeEnv) -> None:
    task = await env.make_task()
    message = AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.EXECUTION_REQUEST,
        payload={"task_id": str(task.id), "steps": "not-a-list"},
        task_id=task.id,
    )
    with pytest.raises(InvalidPayload):
        await env.agent.dispatch_message(message, env.ctx)
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.target_agent == AgentId.ORCHESTR.value
    assert "invalid" in blocker.payload["description"]
    with pytest.raises(InvalidPayload):
        await env.agent.dispatch_message(message, env.ctx)
    assert len(await env.messages(MessageType.BLOCKER)) == 1


def assign(task_id, objective_id, criteria=()) -> AgentMessage:  # type: ignore[no-untyped-def]
    return build_message(
        AgentId.ORCHESTR,
        AgentId.FORGE,
        TaskAssignPayload(
            task_id=task_id,
            objective_id=objective_id,
            title="Implement it",
            assigned_agent=AgentId.FORGE,
            acceptance_criteria=list(criteria),
        ),
        task_id=task_id,
        objective_id=objective_id,
    )


async def test_assignment_without_a_plan_reports_a_blocker(env: ForgeEnv) -> None:
    task = await env.make_task(running=False)
    await env.agent.dispatch_message(assign(task.id, task.objective_id), env.ctx)
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert "implementation_plan_missing" in blocker.payload["description"]


async def test_assignment_for_an_unknown_task_is_an_error(env: ForgeEnv) -> None:
    task = await env.make_task(running=False)
    message = AgentMessage(
        source_agent=AgentId.ORCHESTR,
        target_agent=AgentId.FORGE,
        message_type=MessageType.TASK_ASSIGN,
        payload=assign(uuid4(), task.objective_id).payload,
    )
    with pytest.raises(LookupError):
        await env.agent.dispatch_message(message, env.ctx)


async def test_plan_that_cannot_be_executed_is_reported(env: ForgeEnv) -> None:
    task = await env.make_task(running=False)
    await env.deliver_plan(task, [write("../x.py"), step("format_disk", "nope")])
    [stored] = await env.messages(MessageType.EXECUTION_REQUEST, source=AgentId.ORCHESTR)
    message = AgentMessage.model_validate(
        {
            "source_agent": stored.source_agent,
            "target_agent": stored.target_agent,
            "message_type": stored.message_type,
            "payload": stored.payload,
            "task_id": stored.task_id,
            "objective_id": stored.objective_id,
            "message_id": uuid4(),
        }
    )
    await env.agent.dispatch_message(message, env.ctx)
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert "not executable" in blocker.payload["description"]


def report(task_id, verdict: VerificationVerdict) -> AgentMessage:  # type: ignore[no-untyped-def]
    failed = verdict is not VerificationVerdict.VERIFIED
    return build_message(
        AgentId.LEDGER,
        AgentId.FORGE,
        TestReportPayload(
            test_run_id=uuid4(),
            task_id=task_id,
            suite="forge-plan",
            results=TestCounts(passed=1, failed=1 if failed else 0),
            verdict=verdict,
            summary="one assertion failed",
        ),
        task_id=task_id,
    )


async def test_failed_verification_reports_an_implementation_blocker(env: ForgeEnv) -> None:
    task = await env.make_task(running=False)
    await env.agent.dispatch_message(report(task.id, VerificationVerdict.FAILED), env.ctx)
    [blocker] = await env.messages(MessageType.BLOCKER)
    assert blocker.payload["kind"] == "implementation"
    assert "failed" in blocker.payload["description"]
