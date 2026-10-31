"""WARDEN resuming waiting tasks and the explicit user-authorization gate."""

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from aether.agents.base import TaskResultStatus
from aether.agents.warden import WardenAgent
from aether.communication.contracts import (
    AuthorizationRequestPayload,
    UserDecision,
    UserResponsePayload,
    build_message,
)
from aether.communication.messages import AgentId, MessageType
from aether.security import (
    BlockReason,
    DecisionOutcome,
    Operation,
    OperationBlocked,
    OperationKind,
)
from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    EventRecord,
    TaskRecord,
    TaskStatus,
)


def install(workspace: Path, work: Any) -> Operation:
    argv = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "requests==2.32.3",
        "--index-url",
        "https://pypi.org/simple",
    ]
    return Operation(
        kind=OperationKind.SUBPROCESS,
        target=sys.executable,
        arguments={"argv": argv},
        cwd=str(workspace),
        requested_by=AgentId.FORGE,
        task_id=work.task_id,
        objective_id=work.objective_id,
        declared_requirement_id=work.requirement_id,
    )


EVIDENCE = [{"publisher": "PSF", "sha256": "cd" * 32}]


def config_change(work: Any) -> Operation:
    return Operation(
        kind=OperationKind.CONFIG_CHANGE,
        target="aether.settings:http_timeout",
        arguments={"key": "http_timeout", "value": 45},
        requested_by=AgentId.FORGE,
        task_id=work.task_id,
        objective_id=work.objective_id,
        declared_requirement_id=work.requirement_id,
    )


def user_says(request_id, decision: UserDecision, text: str = "", **ids):
    payload = UserResponsePayload(
        authorization_request_id=request_id, decision=decision, text=text, **ids
    )
    return build_message(AgentId.MUSE, AgentId.WARDEN, payload)


async def _transitions_by_warden(database, task_id) -> list[dict]:
    async with database.sessions() as session:
        rows = await session.scalars(
            select(EventRecord.payload).where(
                EventRecord.aggregate_id == task_id,
                EventRecord.event_type == "task.transitioned",
                EventRecord.actor == "WARDEN",
            )
        )
        return list(rows)


async def _requests(database) -> list[AuthorizationRequestRecord]:
    async with database.sessions() as session:
        return list(await session.scalars(select(AuthorizationRequestRecord)))


# -- resuming after a WARDEN approval ----------------------------------------------------------


async def test_approval_resumes_the_waiting_task(
    agent, ctx, workspace, make_work, to_waiting, task_state, evaluate_message, database
):
    work = await make_work()
    await to_waiting(work.task_id)

    await agent.handle_message(evaluate_message(install(workspace, work), evidence=EVIDENCE), ctx)

    assert await task_state(work.task_id) == TaskStatus.READY.value
    [transition] = await _transitions_by_warden(database, work.task_id)
    assert transition["from_status"] == "waiting_for_approval"
    assert transition["to_status"] == "ready"
    assert transition["details"]["decision"] == "approved"


async def test_decision_made_while_task_still_running_is_applied_by_the_sweep(
    agent, ctx, workspace, make_work, move_task, task_state, evaluate_message, database
):
    work = await make_work()
    await move_task(work.task_id, TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING)

    # WARDEN decides before the worker has recorded waiting_for_approval.
    await agent.handle_message(evaluate_message(install(workspace, work), evidence=EVIDENCE), ctx)
    assert await task_state(work.task_id) == TaskStatus.RUNNING.value

    await move_task(work.task_id, TaskStatus.WAITING_FOR_APPROVAL)
    # A restarted WARDEN finds the durable decision and resumes the task.
    result = await WardenAgent().resume_waiting_tasks(ctx)
    assert result.resumed == [work.task_id]
    assert await task_state(work.task_id) == TaskStatus.READY.value

    # The approval is consumed: a later wait (for another operation) is not resumed by it.
    await move_task(
        work.task_id, TaskStatus.SCHEDULED, TaskStatus.RUNNING, TaskStatus.WAITING_FOR_APPROVAL
    )
    again = await WardenAgent().resume_waiting_tasks(ctx)
    assert again.resumed == [] and again.blocked == []
    assert await task_state(work.task_id) == TaskStatus.WAITING_FOR_APPROVAL.value
    assert len(await _transitions_by_warden(database, work.task_id)) == 1


async def test_execute_task_runs_the_resume_sweep(
    agent, ctx, workspace, make_work, move_task, to_waiting, task_state, evaluate_message, database
):
    work = await make_work()
    await move_task(work.task_id, TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING)
    await agent.handle_message(evaluate_message(install(workspace, work), evidence=EVIDENCE), ctx)
    await move_task(work.task_id, TaskStatus.WAITING_FOR_APPROVAL)

    async with database.sessions() as session:
        task = await session.get(TaskRecord, work.task_id)
    assert task is not None
    result = await agent.execute_task(task, ctx)

    assert result.status is TaskResultStatus.COMPLETED
    assert result.outputs["resumed"] == [str(work.task_id)]


async def test_rejection_blocks_the_waiting_task(
    agent, ctx, workspace, make_work, to_waiting, task_state, evaluate_message
):
    work = await make_work()
    await to_waiting(work.task_id)
    operation = install(workspace, work).model_copy(
        update={"arguments": {"argv": [sys.executable, "-m", "x", "rm -rf /"]}}
    )

    await agent.handle_message(evaluate_message(operation), ctx)

    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value


async def test_expired_approval_never_resumes_the_task(
    agent, ctx, clock, workspace, make_work, move_task, task_state, evaluate_message
):
    work = await make_work()
    await move_task(work.task_id, TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING)
    await agent.handle_message(evaluate_message(install(workspace, work), evidence=EVIDENCE), ctx)
    await move_task(work.task_id, TaskStatus.WAITING_FOR_APPROVAL)

    clock.advance(timedelta(hours=2))
    result = await agent.resume_waiting_tasks(ctx)

    assert result.blocked == [work.task_id]
    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value


# -- explicit user authorization ---------------------------------------------------------------


async def _approved_config_change(agent, ctx, make_work, to_waiting, evaluate_message):
    work = await make_work(title="Raise the http_timeout setting for slow APIs")
    await to_waiting(work.task_id)
    operation = config_change(work)
    await agent.handle_message(evaluate_message(operation), ctx)
    return work, operation


async def test_high_impact_approval_asks_the_user_and_keeps_the_task_waiting(
    agent,
    ctx,
    make_work,
    to_waiting,
    task_state,
    evaluate_message,
    decisions,
    database,
    sent,
    permissions,
):
    work, operation = await _approved_config_change(
        agent, ctx, make_work, to_waiting, evaluate_message
    )

    [decision] = await decisions(operation.input_hash)
    assert decision.decision == DecisionOutcome.APPROVED.value
    assert decision.scope["needs_user_authorization"] is True
    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.PENDING.value
    assert request.security_decision_id == decision.id
    assert request.input_hash == operation.input_hash
    [message] = await sent(MessageType.AUTHORIZATION_REQUEST)
    assert message.target_agent == "MUSE"
    payload = AuthorizationRequestPayload.model_validate(message.payload)
    assert payload.authorization_request_id == request.id
    assert payload.task_id == work.task_id
    assert await task_state(work.task_id) == TaskStatus.WAITING_FOR_APPROVAL.value

    with pytest.raises(OperationBlocked) as blocked:
        await permissions.authorize(operation)
    assert blocked.value.reason is BlockReason.USER_AUTHORIZATION_PENDING
    assert blocked.value.user_request_id == request.id


async def test_silence_and_expiry_never_authorize(
    agent, ctx, clock, make_work, to_waiting, task_state, evaluate_message, database, permissions
):
    work, operation = await _approved_config_change(
        agent, ctx, make_work, to_waiting, evaluate_message
    )
    [request] = await _requests(database)

    # Silence: sweeps and an undecided reply leave everything pending.
    for _ in range(3):
        await agent.resume_waiting_tasks(ctx)
    await agent.handle_message(user_says(request.id, UserDecision.NONE, "sounds fine I guess"), ctx)
    assert await task_state(work.task_id) == TaskStatus.WAITING_FOR_APPROVAL.value
    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.PENDING.value

    # Expiry: the request expires and the task is blocked, never resumed.
    clock.advance(timedelta(hours=2))
    result = await agent.resume_waiting_tasks(ctx)
    assert result.blocked == [work.task_id]
    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.EXPIRED.value
    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value

    # A late approval cannot revive it.
    await agent.handle_message(user_says(request.id, UserDecision.APPROVED, "yes"), ctx)
    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.EXPIRED.value
    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value
    with pytest.raises(OperationBlocked):
        await permissions.authorize(operation)


async def test_late_answer_after_expiry_is_recorded_as_expired(
    agent, ctx, clock, make_work, to_waiting, task_state, evaluate_message, database
):
    work, _ = await _approved_config_change(agent, ctx, make_work, to_waiting, evaluate_message)
    [request] = await _requests(database)

    clock.advance(timedelta(hours=2))
    # The answer arrives before any sweep noticed the deadline.
    await agent.handle_message(user_says(request.id, UserDecision.APPROVED, "yes"), ctx)

    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.EXPIRED.value
    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value


async def test_explicit_user_approval_resumes_the_task_and_unlocks_the_operation(
    agent, ctx, make_work, to_waiting, task_state, evaluate_message, database, permissions
):
    work, operation = await _approved_config_change(
        agent, ctx, make_work, to_waiting, evaluate_message
    )
    [request] = await _requests(database)

    await agent.handle_message(
        user_says(request.id, UserDecision.APPROVED, "yes, go ahead", task_id=work.task_id), ctx
    )

    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.APPROVED.value
    assert await task_state(work.task_id) == TaskStatus.READY.value
    grant = await permissions.authorize(operation)
    assert grant.user_request_id == request.id


async def test_user_denial_blocks_the_task(
    agent, ctx, make_work, to_waiting, task_state, evaluate_message, database, permissions
):
    work, operation = await _approved_config_change(
        agent, ctx, make_work, to_waiting, evaluate_message
    )
    [request] = await _requests(database)

    await agent.handle_message(user_says(request.id, UserDecision.DENIED, "no"), ctx)

    assert await task_state(work.task_id) == TaskStatus.BLOCKED.value
    with pytest.raises(OperationBlocked) as blocked:
        await permissions.authorize(operation)
    assert blocked.value.reason is BlockReason.USER_AUTHORIZATION_DENIED


async def test_user_response_for_another_task_is_ignored(
    agent, ctx, make_work, to_waiting, task_state, evaluate_message, database
):
    work, _ = await _approved_config_change(agent, ctx, make_work, to_waiting, evaluate_message)
    other = await make_work()
    [request] = await _requests(database)

    await agent.handle_message(
        user_says(request.id, UserDecision.APPROVED, "yes", task_id=other.task_id), ctx
    )
    await agent.handle_message(user_says(uuid4(), UserDecision.APPROVED, "yes"), ctx)

    [request] = await _requests(database)
    assert request.status == AuthorizationStatus.PENDING.value
    assert await task_state(work.task_id) == TaskStatus.WAITING_FOR_APPROVAL.value


async def test_user_authorization_request_is_not_duplicated_on_redelivery(
    agent, ctx, make_work, to_waiting, evaluate_message, database, sent
):
    work = await make_work(title="Raise the http_timeout setting for slow APIs")
    await to_waiting(work.task_id)
    message = evaluate_message(config_change(work))

    await agent.handle_message(message, ctx)
    await WardenAgent().handle_message(message, ctx)

    assert len(await _requests(database)) == 1
    assert len(await sent(MessageType.AUTHORIZATION_REQUEST)) == 1
