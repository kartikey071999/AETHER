"""Scenario 9: a WARDEN approval expires while the runtime is down.

Approvals are bound to the exact operation (input hash) and expire. After a restart the WARDEN
resume sweep, which reads only durable state, must block (never resume) a task whose approval
expired in the meantime. The enforcement side must refuse both the expired approval and a
modified operation, and in-memory grants issued before the restart are gone.
"""

import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from reliability_support import (
    RecordingAgent,
    build_engine,
    events_of,
    task_state,
    wait_until,
)

from aether.agents.warden import WardenAgent
from aether.communication.messages import AgentId
from aether.observability.recorder import EventRecorder
from aether.security import (
    ApprovalService,
    BlockReason,
    DecisionOutcome,
    Operation,
    OperationBlocked,
    OperationKind,
    PermissionService,
    SecurityPolicy,
)
from aether.security.policy import default_agent_permissions
from aether.storage.models import RequirementStatus, TaskStatus
from aether.storage.repositories import RequirementRepository, TaskRepository

APPROVAL_TTL = timedelta(minutes=10)


@pytest.fixture
def policy(tmp_path: Path) -> SecurityPolicy:
    for name in ("workspace", "artifacts", "data"):
        (tmp_path / name).mkdir()
    return SecurityPolicy(
        workspace_dir=tmp_path / "workspace",
        artifact_dir=tmp_path / "artifacts",
        data_dir=tmp_path / "data",
        allowed_executables=[sys.executable],
        allowed_hosts=["pypi.org"],
        agent_permissions=dict(default_agent_permissions()),
        max_approval_ttl=timedelta(hours=1),
        authorization_ttl=timedelta(minutes=5),
    )


@pytest.fixture
def approvals(sessions, policy, clock) -> ApprovalService:
    return ApprovalService(sessions, EventRecorder(), policy, clock=clock)


async def _waiting_task(sessions, objective_id: UUID) -> tuple[UUID, UUID]:
    """FORGE's task, parked waiting for WARDEN after its tool call needed approval."""
    requirements = RequirementRepository(sessions)
    requirement_id = await requirements.create(
        objective_id=objective_id,
        title="Run the data export script",
        description="Run the export with the project interpreter.",
        requirement_type="functional",
        created_by="SCOUT",
        acceptance_criteria=["AC1: the export runs"],
    )
    await requirements.set_status(requirement_id, RequirementStatus.APPROVED, actor="SCOUT")
    tasks = TaskRepository(sessions)
    task_id = await tasks.create(
        objective_id=objective_id,
        title="Run export",
        assigned_agent="FORGE",
        requirement_id=requirement_id,
    )
    for state in (TaskStatus.READY, TaskStatus.SCHEDULED, TaskStatus.RUNNING):
        await tasks.transition(task_id, state, actor="ORCHESTR")
    return task_id, requirement_id


def _operation(policy, objective_id, task_id, requirement_id, code="print('export')"):
    return Operation(
        kind=OperationKind.SUBPROCESS,
        target=sys.executable,
        arguments={"argv": [sys.executable, "-c", code]},
        cwd=str(policy.workspace_dir),
        requested_by=AgentId.FORGE,
        task_id=task_id,
        objective_id=objective_id,
        declared_requirement_id=requirement_id,
    )


async def _approve_then_wait(sessions, approvals, clock, operation, task_id) -> None:
    # WARDEN decides while FORGE's attempt is still running, then the task parks.
    await approvals.record_decision(
        AgentId.WARDEN,
        operation,
        DecisionOutcome.APPROVED,
        rationale="allowlisted interpreter with a fixed argv",
        evidence=[{"check": "recovery-test"}],
        expires_at=clock() + APPROVAL_TTL,
    )
    await TaskRepository(sessions).transition(
        task_id, TaskStatus.WAITING_FOR_APPROVAL, actor="RUNTIME"
    )


async def _run_engine(sessions, clock, approvals, forge: RecordingAgent, until) -> None:
    engine = build_engine(
        sessions,
        [WardenAgent(), forge],
        clock=clock,
        agent_services={AgentId.WARDEN: {"approvals": approvals}},
    )
    await engine.start()
    try:
        await wait_until(until, timeout=10.0)
    finally:
        await engine.stop()


async def test_approval_that_expired_during_downtime_blocks_the_task_after_restart(
    sessions, objective_id, clock, policy, approvals
) -> None:
    task_id, requirement_id = await _waiting_task(sessions, objective_id)
    operation = _operation(policy, objective_id, task_id, requirement_id)
    await _approve_then_wait(sessions, approvals, clock, operation, task_id)

    clock.advance((APPROVAL_TTL + timedelta(minutes=1)).total_seconds())  # runtime was down
    forge = RecordingAgent()
    await _run_engine(
        sessions, clock, approvals, forge, lambda: _is(sessions, task_id, TaskStatus.BLOCKED)
    )

    assert forge.executed == []
    blocked = [
        event
        for event in await events_of(sessions, "task.transitioned", task_id)
        if event.payload["to_status"] == TaskStatus.BLOCKED.value
    ]
    assert len(blocked) == 1
    assert blocked[0].actor == AgentId.WARDEN.value
    assert blocked[0].payload["details"]["reason"] == (
        "the WARDEN approval expired before the task resumed"
    )


async def test_approval_still_valid_after_restart_resumes_the_task(
    sessions, objective_id, clock, policy, approvals
) -> None:
    task_id, requirement_id = await _waiting_task(sessions, objective_id)
    operation = _operation(policy, objective_id, task_id, requirement_id)
    await _approve_then_wait(sessions, approvals, clock, operation, task_id)

    clock.advance((APPROVAL_TTL / 2).total_seconds())
    forge = RecordingAgent()
    await _run_engine(
        sessions, clock, approvals, forge, lambda: _is(sessions, task_id, TaskStatus.COMPLETED)
    )

    assert forge.executed == [task_id]


async def test_enforcement_after_restart_rejects_expired_and_modified_operations(
    sessions, objective_id, clock, policy, approvals
) -> None:
    task_id, requirement_id = await _waiting_task(sessions, objective_id)
    operation = _operation(policy, objective_id, task_id, requirement_id)
    await _approve_then_wait(sessions, approvals, clock, operation, task_id)
    before_restart = PermissionService(sessions, EventRecorder(), policy, clock=clock)
    grant = await before_restart.authorize(operation)

    # The process restarts: a fresh PermissionService holds no in-memory grants.
    after_restart = PermissionService(sessions, EventRecorder(), policy, clock=clock)
    with pytest.raises(OperationBlocked) as stale:
        await after_restart.redeem(grant, operation)
    assert stale.value.reason is BlockReason.INVALID_AUTHORIZATION

    modified = _operation(policy, objective_id, task_id, requirement_id, code="print('other')")
    with pytest.raises(OperationBlocked) as different:
        await after_restart.authorize(modified)
    assert different.value.reason is BlockReason.APPROVAL_MISSING

    clock.advance((APPROVAL_TTL + timedelta(seconds=1)).total_seconds())
    with pytest.raises(OperationBlocked) as expired:
        await after_restart.authorize(operation)
    assert expired.value.reason is BlockReason.APPROVAL_EXPIRED


async def _is(sessions, task_id, state: TaskStatus) -> bool:
    return await task_state(sessions, task_id) is state
