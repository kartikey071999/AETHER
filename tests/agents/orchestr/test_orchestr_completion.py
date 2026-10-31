"""Completion detection and the final user update."""

from uuid import UUID, uuid4

import pytest
from orchestr_helpers import Harness

from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import (
    AcceptanceResult,
    ContractViolation,
    TestCounts,
    TestReportPayload,
    VerificationVerdict,
)
from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus

CRITERION = "PDF text is extracted correctly"


async def complete_all(harness: Harness, objective_id: UUID) -> None:
    """Drive every task to COMPLETED the way the worker pool would, releasing dependents."""
    await harness.approve_requirement(objective_id)
    for _ in range(12):
        ready = [
            task
            for task in (await harness.tasks(objective_id)).values()
            if task.state == TaskStatus.READY.value
        ]
        if not ready:
            return
        for task in ready:
            await harness.run_to(task.id)
        await harness.tick()


def report(
    verdict: VerificationVerdict = VerificationVerdict.VERIFIED,
    *,
    criterion: str = CRITERION,
    task_id: UUID | None = None,
) -> TestReportPayload:
    passed = verdict is VerificationVerdict.VERIFIED
    return TestReportPayload(
        test_run_id=uuid4(),
        task_id=task_id,
        suite="acceptance",
        results=TestCounts(passed=3 if passed else 1, failed=0 if passed else 2),
        acceptance_results=[AcceptanceResult(criterion=criterion, passed=passed, evidence="ran")],
        verdict=verdict,
    )


async def test_finished_tasks_alone_do_not_complete_an_objective(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    tasks = await harness.tasks(objective_id)
    assert {task.state for task in tasks.values()} == {TaskStatus.COMPLETED.value}
    assert (await harness.get_objective(objective_id)).status == "active"
    assert harness.bus.of_type(MessageType.OBJECTIVE_COMPLETED) == []


async def test_verified_report_completes_the_objective(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)

    assert (await harness.get_objective(objective_id)).status == "completed"
    completed = harness.bus.of_type(MessageType.OBJECTIVE_COMPLETED, target=AgentId.LEDGER)
    assert len(completed) == 1
    finals = harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
    assert len(finals) == 1


async def test_final_update_separates_verified_from_uncertain(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)

    [final] = harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
    payload = final.payload if isinstance(final.payload, dict) else final.payload.model_dump()
    assert payload["verified"] == [CRITERION]
    # Spec section 10: MUSE must not present untested behaviour as verified.
    assert payload["uncertain"]
    assert CRITERION not in payload["uncertain"]


async def test_completion_is_published_once_even_when_redelivered(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    message = await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)
    await harness.agent.dispatch_message(message, harness.ctx)
    await harness.tick()
    assert len(harness.bus.of_type(MessageType.OBJECTIVE_COMPLETED)) == 1
    assert len(harness.bus.of_type(MessageType.COMMUNICATION_REQUEST)) == 1


async def test_report_for_other_criteria_does_not_complete(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    await harness.send(
        AgentId.LEDGER, report(criterion="Something else entirely"), objective_id=objective_id
    )
    assert (await harness.get_objective(objective_id)).status == "active"


async def test_every_criterion_must_be_verified(harness: Harness) -> None:
    objective_id = await harness.objective(criteria=[CRITERION, "Tables are preserved"])
    await complete_all(harness, objective_id)
    await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)
    assert (await harness.get_objective(objective_id)).status == "active"
    await harness.send(
        AgentId.LEDGER, report(criterion="Tables are preserved"), objective_id=objective_id
    )
    assert (await harness.get_objective(objective_id)).status == "completed"


async def test_incomplete_tasks_block_completion_despite_verified_report(
    harness: Harness,
) -> None:
    objective_id = await harness.objective()
    await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)
    assert (await harness.get_objective(objective_id)).status == "active"


async def test_only_ledger_can_submit_test_reports(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    with pytest.raises(ContractViolation):
        await harness.send(AgentId.FORGE, report(), objective_id=objective_id)
    assert (await harness.get_objective(objective_id)).status == "active"


async def test_failed_report_appends_rework_then_escalates(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    before = len(await harness.tasks(objective_id))

    await harness.send(
        AgentId.LEDGER, report(VerificationVerdict.FAILED), objective_id=objective_id
    )
    tasks = await harness.tasks(objective_id)
    assert len(tasks) > before
    assert any(key.startswith("rework.") for key in tasks)
    assert any(key.startswith("reverify.") for key in tasks)
    assert (await harness.get_objective(objective_id)).status == "active"
    # Rework and re-verification tasks keep the requirement the original tasks were linked to.
    follow_ups = [task for key, task in tasks.items() if key.startswith(("rework.", "reverify."))]
    assert follow_ups and all(task.requirement_id is not None for task in follow_ups)
    assert {task.requirement_id for task in follow_ups} == {tasks["implementation"].requirement_id}
    for _ in range(3):
        await harness.send(
            AgentId.LEDGER, report(VerificationVerdict.FAILED), objective_id=objective_id
        )
    assert (await harness.get_objective(objective_id)).status == "blocked"
    escalations = harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
    assert len(escalations) == 1


async def test_rework_that_would_exceed_the_task_limit_escalates(harness: Harness) -> None:
    objective_id = await harness.objective()
    await complete_all(harness, objective_id)
    before = len(await harness.tasks(objective_id))
    # Room for one more task, but rework adds two (rework + re-verification).
    harness.use_limits(OrchestrLimits(llm_refinement=False, max_tasks_per_objective=before + 1))

    await harness.send(
        AgentId.LEDGER, report(VerificationVerdict.FAILED), objective_id=objective_id
    )

    assert len(await harness.tasks(objective_id)) == before
    assert (await harness.get_objective(objective_id)).status == "blocked"
    [escalation] = await harness.store.keyed_decisions("orchestr:escalated:")
    assert "could not be extended" in escalation.rationale


async def test_generated_objective_completion_is_silent_to_the_user(harness: Harness) -> None:
    objective_id = await harness.objective(
        "Investigate a PDF tool capability",
        source=AgentId.SCOUT,
        criteria=[CRITERION],
        generated=True,
        rationale="gap in extraction",
        stopping_conditions=["no viable tool"],
        expected_benefit="better extraction",
        estimated_cost=5.0,
    )
    await complete_all(harness, objective_id)
    await harness.send(AgentId.LEDGER, report(), objective_id=objective_id)
    assert (await harness.get_objective(objective_id)).status == "completed"
    assert harness.bus.of_type(MessageType.COMMUNICATION_REQUEST) == []
