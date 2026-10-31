"""Completion detection: an objective completes only when its criteria are verified."""

import hashlib
from uuid import UUID

from aether.agents.orchestr.blockers import escalate_objective
from aether.agents.orchestr.communication import UpdateEvent, UpdateKind
from aether.agents.orchestr.env import Env
from aether.agents.orchestr.planning import add_rework
from aether.agents.orchestr.store import ACTOR, stable_id
from aether.communication.contracts import (
    AcceptanceResult,
    ObjectiveCompletedPayload,
    TestReportPayload,
    VerificationVerdict,
    build_message,
)
from aether.communication.messages import AgentId, AgentMessage
from aether.storage.models import ObjectiveRecord, TaskStatus

SATISFIED = "criterion_satisfied"
FAILED = "criterion_failed"


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def criterion_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:16]


async def satisfied_criteria(env: Env, objective: ObjectiveRecord) -> dict[str, str]:
    """Criterion hash -> evidence for criteria whose latest verification result passed."""
    latest: dict[str, tuple[str, str]] = {}
    for view in await env.store.keyed_decisions(f"orchestr:criterion:{objective.id}:"):
        parts = (view.key or "").split(":")
        if len(parts) >= 4:
            latest[parts[3]] = (view.decision, view.evidence[0] if view.evidence else "")
    return {name: evidence for name, (state, evidence) in latest.items() if state == SATISFIED}


async def _objective_for(
    env: Env, objective_id: UUID | None, task_id: UUID | None
) -> ObjectiveRecord | None:
    if objective_id is None and task_id is not None:
        task = await env.store.get_task(task_id)
        objective_id = task.objective_id if task is not None else None
    return await env.store.get_objective(objective_id) if objective_id else None


async def handle_test_report(env: Env, payload: TestReportPayload, message: AgentMessage) -> None:
    objective = await _objective_for(env, message.objective_id, payload.task_id)
    run = payload.test_run_id
    if objective is None:
        await env.store.record_decision(
            "test_report_unattributed",
            "test report does not reference a known objective or task",
            key=f"orchestr:test-report:{run}",
        )
        return
    counts = payload.results
    await env.store.record_decision(
        "test_report",
        f"verification verdict {payload.verdict.value}",
        key=f"orchestr:test-report:{run}",
        evidence=[
            payload.verdict.value,
            f"passed={counts.passed} failed={counts.failed} errors={counts.errors}",
        ],
        objective_id=objective.id,
        task_id=payload.task_id,
        correlation_id=message.correlation_id,
    )
    known = {criterion_hash(item) for item in objective.completion_criteria}
    for result in payload.acceptance_results:
        digest = criterion_hash(result.criterion)
        if digest not in known:
            continue
        verified = result.passed and payload.verdict is VerificationVerdict.VERIFIED
        await env.store.record_decision(
            SATISFIED if verified else FAILED,
            "criterion verified by independent test report"
            if verified
            else "criterion not satisfied by test report",
            key=f"orchestr:criterion:{objective.id}:{digest}:{run}",
            evidence=[str(run)],
            objective_id=objective.id,
        )
    if payload.verdict is VerificationVerdict.FAILED and objective.status == "active":
        await _rework(env, objective, payload)
    refreshed = await env.store.get_objective(objective.id)
    if refreshed is not None:
        await evaluate_completion(env, refreshed)


async def _rework(env: Env, objective: ObjectiveRecord, payload: TestReportPayload) -> None:
    marker = f"orchestr:replan:{objective.id}:{payload.test_run_id}"
    if await env.store.get_decision(marker) is not None:
        return
    replans = await env.store.decisions(objective_id=objective.id, decision="replan")
    if len(replans) >= env.limits.max_replans_per_objective:
        await escalate_objective(
            env,
            objective,
            key=f"replans:{objective.id}",
            reason="independent verification failed repeatedly; automatic rework limit reached",
        )
        return
    if not await add_rework(env, objective, payload.test_run_id.hex[:8]):
        await escalate_objective(
            env,
            objective,
            key=f"replan-failed:{payload.test_run_id}",
            reason="verification failed and the plan could not be extended with rework",
        )
        return
    await env.store.record_decision(
        "replan",
        "verification failed; FORGE rework and LEDGER re-verification appended",
        key=marker,
        objective_id=objective.id,
    )


async def evaluate_completion(env: Env, objective: ObjectiveRecord) -> bool:
    """Complete the objective iff every criterion is verified and every live task is done."""
    if objective.status != "active" or not objective.completion_criteria:
        return False
    tasks = [
        task
        for task in await env.store.tasks_of(objective.id)
        if TaskStatus(task.state) is not TaskStatus.CANCELLED
    ]
    if not tasks or any(TaskStatus(task.state) is not TaskStatus.COMPLETED for task in tasks):
        return False
    satisfied = await satisfied_criteria(env, objective)
    criteria = list(objective.completion_criteria)
    if any(criterion_hash(item) not in satisfied for item in criteria):
        return False

    payload = ObjectiveCompletedPayload(
        objective_id=objective.id,
        summary=f"All {len(tasks)} tasks completed and every completion criterion was verified",
        criteria_results=[
            AcceptanceResult(
                criterion=item,
                passed=True,
                evidence=f"test run {satisfied[criterion_hash(item)]}",
            )
            for item in criteria
        ],
    )
    await env.publish(
        build_message(
            AgentId.ORCHESTR,
            AgentId.LEDGER,
            payload,
            message_id=stable_id("objective-completed", objective.id),
            objective_id=objective.id,
            priority=objective.priority,
        )
    )
    await env.send_user_update(
        UpdateEvent(
            UpdateKind.OBJECTIVE_COMPLETED, user_originated=not objective.generated_by_agent
        ),
        key=f"final:{objective.id}",
        what_happened=f"Objective '{objective.title}' is complete.",
        objective=objective,
        results=[{"objective": objective.title, "tasks_completed": len(tasks)}],
        verified=criteria,
        uncertain=["Behaviour outside the listed completion criteria was not independently tested"],
    )
    await env.store.objectives.set_status(
        objective.id, "completed", actor=ACTOR, reason="all completion criteria verified"
    )
    await env.store.record_decision(
        "objective_completed",
        "all tasks completed and all criteria verified",
        key=f"orchestr:completed:{objective.id}",
        objective_id=objective.id,
    )
    return True
