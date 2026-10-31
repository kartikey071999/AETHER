"""Outbound messages; ids are deterministic so a redelivered task never double-publishes."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from aether.agents.forge.planning import forge_id, relative_path
from aether.agents.forge.schemas import ForgePlan, PlanDefect
from aether.communication.contracts import (
    ROUTES,
    BlockerKind,
    BlockerPayload,
    ExecutionResultPayload,
    ProgressUpdatePayload,
    SecurityEvaluatePayload,
    VerificationRequestPayload,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage, MessageType
from aether.security.errors import BlockReason
from aether.security.operations import Operation
from aether.storage.models import RunStatus, TaskRecord


def blocker_message(
    task: TaskRecord,
    *,
    kind: BlockerKind,
    route_to: AgentId,
    description: str,
    key: str,
    attempts: int = 0,
    evidence: Sequence[dict[str, Any]] = (),
) -> AgentMessage:
    payload = BlockerPayload(
        blocker_id=forge_id("blocker-id", task.id, key),
        task_id=task.id,
        objective_id=task.objective_id,
        kind=kind,
        description=description[:2000],
        route_to=route_to,
        attempts=attempts,
        evidence=list(evidence),
    )
    # WARDEN and MUSE cannot receive blockers directly; ORCHESTR relays them per `route_to`.
    target = route_to if route_to in ROUTES[MessageType.BLOCKER] else AgentId.ORCHESTR
    return build_message(
        AgentId.FORGE,
        target,
        payload,
        message_id=forge_id("blocker", task.id, key),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=task.requirement_id,
    )


def evaluate_message(task: TaskRecord, operation: Operation, reason: str) -> AgentMessage:
    payload = SecurityEvaluatePayload(
        operation=operation,
        input_hash=operation.input_hash,
        requirement_id=operation.declared_requirement_id,
        evidence=[{"type": "forge_preflight", "reason": reason, "operation": operation.describe()}],
        requested_by=AgentId.FORGE,
        task_id=task.id,
        block_reason=BlockReason.APPROVAL_MISSING,
    )
    return build_message(
        AgentId.FORGE,
        AgentId.WARDEN,
        payload,
        message_id=forge_id("evaluate", task.id, operation.input_hash),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=operation.declared_requirement_id,
        requires_response=True,
    )


def result_message(
    task: TaskRecord,
    plan: ForgePlan,
    *,
    status: RunStatus,
    run_ids: Sequence[str],
    artifacts: Sequence[str],
    evidence: Sequence[dict[str, Any]],
    outputs: dict[str, Any],
    error: str | None = None,
) -> AgentMessage:
    payload = ExecutionResultPayload(
        task_id=task.id,
        requirement_id=plan.requirement_id or task.requirement_id,
        execution_run_ids=[UUID(item) for item in run_ids],
        status=status,
        outputs=outputs,
        artifacts=list(artifacts),
        evidence=list(evidence),
        error=error,
    )
    return build_message(
        AgentId.FORGE,
        AgentId.LEDGER,
        payload,
        message_id=forge_id("execution-result", task.id, plan.plan_id),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=task.requirement_id,
    )


def _test_files(task: TaskRecord, plan: ForgePlan) -> list[str]:
    """Pytest files the plan wrote, relative to the task workspace."""
    found: list[str] = []
    for step in plan.steps:
        if step.tool != "write_file":
            continue
        try:
            path = relative_path(step.arguments.get("path"), task.id)
        except PlanDefect:
            continue
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
            found.append(path)
    return list(dict.fromkeys(found))


def verification_message(
    task: TaskRecord,
    plan: ForgePlan,
    *,
    requirement_id: UUID,
    criteria: Sequence[str],
    run_ids: Sequence[str],
    artifacts: Sequence[str],
    evidence: Sequence[dict[str, Any]],
) -> AgentMessage:
    """A request for independent verification; FORGE never declares anything verified itself.

    The suite names the test files FORGE wrote; LEDGER runs them itself inside the task workspace.
    """
    tests = _test_files(task, plan)
    payload = VerificationRequestPayload(
        task_id=task.id,
        requirement_id=requirement_id,
        suite=" ".join(tests) if tests else f"forge-plan-{str(plan.plan_id)[:8]}",
        acceptance_criteria=list(criteria),
        execution_run_ids=[UUID(item) for item in run_ids],
        artifacts=list(artifacts),
        evidence=[*evidence, {"workspace": str(task.id)}],
    )
    return build_message(
        AgentId.FORGE,
        AgentId.LEDGER,
        payload,
        message_id=forge_id("verification", task.id, plan.plan_id),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=task.requirement_id,
    )


def promotion_message(
    task: TaskRecord, plan: ForgePlan, *, details: dict[str, Any]
) -> AgentMessage:
    """Asks ORCHESTR to route a runtime self-change for review; FORGE never deploys it."""
    payload = ProgressUpdatePayload(
        task_id=task.id,
        objective_id=task.objective_id,
        summary="FORGE promotion request: runtime change prepared in a separate workspace copy",
        percent_complete=100.0,
        details={"forge_promotion_request": True, "plan_id": str(plan.plan_id), **details},
    )
    return build_message(
        AgentId.FORGE,
        AgentId.ORCHESTR,
        payload,
        message_id=forge_id("promotion", task.id, plan.plan_id),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=task.requirement_id,
        requires_response=True,
    )
