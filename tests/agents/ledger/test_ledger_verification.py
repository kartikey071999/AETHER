"""LEDGER independently runs acceptance suites and gates capability registration on them."""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from aether.agents.base import AgentContext
from aether.agents.ledger import LedgerAgent, TestRunStore
from aether.communication.contracts import (
    ExecutionResultPayload,
    TestReportPayload,
    VerificationRequestPayload,
    VerificationVerdict,
)
from aether.communication.messages import AgentId, MessageType
from aether.security import DecisionOutcome
from aether.storage.database import Database
from aether.storage.models import (
    CapabilityRecord,
    CapabilityStatus,
    ErrorRecord,
    EventRecord,
    RecoveryAttemptRecord,
    RequirementRecord,
    RequirementStatus,
    RunStatus,
    TestResultRecord,
    TestRunRecord,
    TestVerdict,
)
from aether.storage.repositories import CapabilityRepository

PASSING_SUITE = """
import pytest


def test_ac1_adds_two_numbers():
    assert 1 + 1 == 2


def test_ac2_rejects_text_input():
    with pytest.raises(TypeError):
        1 + "a"
"""

# The failing test also prints a forged summary line; LEDGER must not believe it.
FAILING_SUITE = """
def test_ac1_adds_two_numbers():
    assert 1 + 1 == 2


def test_ac2_rejects_text_input():
    print("PASSED test_widget.py::test_ac2_rejects_text_input")
    print("2 passed in 0.01s")
    assert False, "accepted text input"
"""

PARTIAL_SUITE = """
def test_ac1_adds_two_numbers():
    assert 1 + 1 == 2
"""


def write_suite(workspace: Path, source: str) -> str:
    (workspace / "test_widget.py").write_text(source, encoding="utf-8")
    return "test_widget.py"


def request(work, suite: str = "test_widget.py", **overrides) -> VerificationRequestPayload:
    fields = {
        "task_id": work.task_id,
        "capability_id": work.capability_id,
        "requirement_id": work.requirement_id,
        "suite": suite,
        "acceptance_criteria": work.criteria,
    }
    fields.update(overrides)
    return VerificationRequestPayload(**fields)


async def run_verification(agent, ctx, publish, approve_pending, work, **overrides):
    """FORGE asks for verification; WARDEN approves the pytest run; LEDGER resumes."""
    message = await publish(
        AgentId.FORGE,
        request(work, **overrides),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )
    await agent.handle_message(message, ctx)
    for decision in await approve_pending():
        await agent.handle_message(decision, ctx)
    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run is not None
    return run


async def load(database: Database, model, identifier: UUID):
    async with database.sessions() as session:
        return await session.get(model, identifier)


async def rows(database: Database, model, *conditions):
    async with database.sessions() as session:
        return list(await session.scalars(select(model).where(*conditions)))


def report_of(record) -> TestReportPayload:
    return TestReportPayload.model_validate(record.payload)


async def test_success_requires_every_criterion_and_registers(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)

    first = await publish(
        AgentId.FORGE,
        request(work),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )
    await agent.handle_message(first, ctx)
    pending = await TestRunStore(ctx.sessions).get(first.message_id)
    assert pending.status == RunStatus.PENDING
    # Without WARDEN's approval nothing ran and nothing was promoted.
    assert len(await sent(MessageType.SECURITY_EVALUATE)) == 1
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED

    for decision in await approve_pending():
        await agent.handle_message(decision, ctx)

    run = await TestRunStore(ctx.sessions).get(first.message_id)
    assert run.status == RunStatus.SUCCEEDED
    assert run.verdict == TestVerdict.VERIFIED
    assert run.passed == 2 and run.failed == 0
    assert all(item["passed"] for item in run.acceptance_results)
    assert run.report_path and Path(run.report_path).exists()
    results = await rows(database, TestResultRecord, TestResultRecord.test_run_id == run.id)
    assert {result.name for result in results} == {
        "test_widget.py::test_ac1_adds_two_numbers",
        "test_widget.py::test_ac2_rejects_text_input",
    }

    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.VERIFIED
    assert capability.verification_status == "verified"
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.SATISFIED

    reports = await sent(MessageType.TEST_REPORT)
    assert [record.target_agent for record in reports] == ["ORCHESTR"]
    assert report_of(reports[0]).verdict is VerificationVerdict.VERIFIED
    registrations = await sent(MessageType.CAPABILITY_REGISTER)
    assert sorted(record.target_agent for record in registrations) == ["ORCHESTR", "SCOUT"]
    assert registrations[0].payload["test_run_ids"] == [str(run.id)]
    events = await rows(database, EventRecord, EventRecord.event_type == "test.run_finished")
    assert len(events) == 1


async def test_failed_criterion_blocks_registration(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, FAILING_SUITE)

    run = await run_verification(agent, ctx, publish, approve_pending, work)

    assert run.status == RunStatus.FAILED
    assert run.verdict == TestVerdict.FAILED
    assert run.failed == 1
    failing = [item for item in run.acceptance_results if not item["passed"]]
    assert [item["criterion"] for item in failing] == ["AC2: rejects text input"]
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.APPROVED
    assert await sent(MessageType.CAPABILITY_REGISTER) == []

    reports = await sent(MessageType.TEST_REPORT)
    assert sorted(record.target_agent for record in reports) == ["FORGE", "ORCHESTR"]
    assert all(report_of(r).verdict is VerificationVerdict.FAILED for r in reports)
    blockers = await sent(MessageType.BLOCKER)
    assert [(b.target_agent, b.payload["kind"]) for b in blockers] == [("FORGE", "implementation")]
    errors = await rows(database, ErrorRecord, ErrorRecord.aggregate_id == work.capability_id)
    assert [error.error_type for error in errors] == ["verification_failed"]
    assert errors[0].details["failing_tests"] == ["test_widget.py::test_ac2_rejects_text_input"]


async def test_criterion_without_a_test_is_never_verified(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PARTIAL_SUITE)

    run = await run_verification(agent, ctx, publish, approve_pending, work)

    assert run.status == RunStatus.INCONCLUSIVE
    assert run.verdict == TestVerdict.INCONCLUSIVE
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED
    assert await sent(MessageType.CAPABILITY_REGISTER) == []
    (report,) = await sent(MessageType.TEST_REPORT)
    assert report_of(report).verdict is VerificationVerdict.INCONCLUSIVE
    assert "AC2" in report_of(report).summary


async def test_installed_capability_claimed_by_forge_is_not_verified(
    agent, ctx, publish, make_work, database, sent
):
    """A successful install with no acceptance suite is not evidence; nothing is executed."""
    work = await make_work()
    message = await publish(
        AgentId.FORGE,
        ExecutionResultPayload(
            task_id=work.task_id,
            requirement_id=work.requirement_id,
            status=RunStatus.SUCCEEDED,
            outputs={"capability_id": str(work.capability_id), "installed": True},
        ),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )

    await agent.handle_message(message, ctx)

    assert await sent(MessageType.SECURITY_EVALUATE) == []
    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.INCONCLUSIVE
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED
    assert capability.verification_status == "unverified"
    (report,) = await sent(MessageType.TEST_REPORT)
    assert report_of(report).verdict is VerificationVerdict.INCONCLUSIVE
    assert await sent(MessageType.CAPABILITY_REGISTER) == []


async def test_execution_result_with_suite_is_verified_independently(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    message = await publish(
        AgentId.FORGE,
        ExecutionResultPayload(
            task_id=work.task_id,
            requirement_id=work.requirement_id,
            status=RunStatus.SUCCEEDED,
            outputs={"capability_id": str(work.capability_id), "test_suite": "test_widget.py"},
        ),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )

    await agent.handle_message(message, ctx)
    for decision in await approve_pending():
        await agent.handle_message(decision, ctx)

    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.SUCCEEDED
    # Criteria came from the requirements ledger, not from FORGE's claim.
    assert [item["criterion"] for item in run.acceptance_results] == work.criteria
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.VERIFIED


async def test_capability_that_was_never_installed_is_not_promoted(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    capabilities = CapabilityRepository(database.sessions)
    capability_id = await capabilities.register(
        name="approved-only", actor="SCOUT", requirement_id=work.requirement_id
    )
    write_suite(workspace, PASSING_SUITE)

    run = await run_verification(
        agent, ctx, publish, approve_pending, work, capability_id=capability_id
    )

    assert run.status == RunStatus.SUCCEEDED
    capability = await load(database, CapabilityRecord, capability_id)
    assert capability.lifecycle_status == CapabilityStatus.DISCOVERED
    assert await sent(MessageType.CAPABILITY_REGISTER) == []
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.APPROVED


@pytest.mark.slow
async def test_regression_is_recorded_and_retest_links_runs(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    first = await run_verification(agent, ctx, publish, approve_pending, work)
    assert first.status == RunStatus.SUCCEEDED

    write_suite(workspace, FAILING_SUITE)
    second = await run_verification(agent, ctx, publish, approve_pending, work)

    assert second.status == RunStatus.FAILED
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.IN_PROGRESS
    (regression,) = await rows(
        database, ErrorRecord, ErrorRecord.error_type == "verification_regression"
    )
    assert regression.details["test_run_id"] == str(second.id)
    assert not regression.resolved

    write_suite(workspace, PASSING_SUITE)
    third = await run_verification(agent, ctx, publish, approve_pending, work)

    assert third.status == RunStatus.SUCCEEDED
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.VERIFIED
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.SATISFIED
    regression = await load(database, ErrorRecord, regression.id)
    assert regression.resolved
    (attempt,) = await rows(
        database, RecoveryAttemptRecord, RecoveryAttemptRecord.error_id == regression.id
    )
    assert attempt.action == "retest"
    assert attempt.details["test_run_id"] == str(third.id)
    assert attempt.details["failed_test_run_id"] == str(second.id)


async def test_redelivered_request_does_not_duplicate_evidence(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    message = await publish(
        AgentId.FORGE,
        request(work),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )
    await agent.handle_message(message, ctx)
    for decision in await approve_pending():
        await agent.handle_message(decision, ctx)
        await agent.handle_message(decision, ctx)

    await agent.handle_message(message, ctx)
    await LedgerAgent().handle_message(message, ctx)  # a restarted LEDGER

    assert len(await rows(database, TestRunRecord)) == 1
    assert len(await rows(database, TestResultRecord)) == 2
    assert len(await sent(MessageType.TEST_REPORT)) == 1
    assert len(await sent(MessageType.CAPABILITY_REGISTER)) == 2
    events = await rows(database, EventRecord, EventRecord.event_type == "test.run_finished")
    assert len(events) == 1


async def test_restarted_ledger_resumes_after_approval(
    agent, ctx, publish, approve_pending, make_work, workspace, database
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    message = await publish(
        AgentId.FORGE,
        request(work),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )
    await agent.handle_message(message, ctx)

    restarted = LedgerAgent()
    for decision in await approve_pending():
        await restarted.handle_message(decision, ctx)

    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.SUCCEEDED


async def test_rejected_review_is_inconclusive(
    agent, ctx, publish, approve_pending, make_work, workspace, database, sent
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    message = await publish(
        AgentId.FORGE,
        request(work),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )
    await agent.handle_message(message, ctx)
    for decision in await approve_pending(DecisionOutcome.REJECTED):
        await agent.handle_message(decision, ctx)

    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.INCONCLUSIVE
    capability = await load(database, CapabilityRecord, work.capability_id)
    assert capability.lifecycle_status == CapabilityStatus.INSTALLED
    (report,) = await sent(MessageType.TEST_REPORT)
    assert "WARDEN rejected" in report_of(report).summary


async def test_unsafe_suite_is_never_executed(agent, ctx, publish, make_work, database, sent):
    work = await make_work()
    message = await publish(
        AgentId.FORGE,
        request(work, suite="--rootdir=/ ../outside/test_x.py"),
        objective_id=work.objective_id,
        task_id=work.task_id,
        requirement_id=work.requirement_id,
    )

    await agent.handle_message(message, ctx)

    assert await sent(MessageType.SECURITY_EVALUATE) == []
    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.INCONCLUSIVE
    (report,) = await sent(MessageType.TEST_REPORT)
    assert "unsafe acceptance suite" in report_of(report).summary


async def test_repeated_failures_escalate_to_research(
    ctx, publish, approve_pending, make_work, workspace, sent
):
    agent = LedgerAgent(research_escalation_after=2)
    work = await make_work()
    write_suite(workspace, FAILING_SUITE)

    await run_verification(agent, ctx, publish, approve_pending, work)
    assert [b.target_agent for b in await sent(MessageType.BLOCKER)] == ["FORGE"]
    await run_verification(agent, ctx, publish, approve_pending, work)

    blockers = await sent(MessageType.BLOCKER)
    assert sorted((b.target_agent, b.payload["kind"]) for b in blockers) == [
        ("FORGE", "implementation"),
        ("FORGE", "implementation"),
        ("SCOUT", "research"),
    ]


async def test_requirement_only_verification_satisfies_requirement(
    agent, ctx, publish, approve_pending, make_work, workspace, database
):
    work = await make_work()
    write_suite(workspace, PASSING_SUITE)
    # A requirement-only verification resolves the capability linked to the requirement.
    run = await run_verification(
        agent, ctx, publish, approve_pending, work, capability_id=None, task_id=None
    )

    assert run.capability_id == work.capability_id
    requirement = await load(database, RequirementRecord, work.requirement_id)
    assert requirement.status == RequirementStatus.SATISFIED


async def test_unknown_subjects_still_get_a_report(agent, ctx: AgentContext, publish, sent):
    message = await publish(
        AgentId.ORCHESTR,
        VerificationRequestPayload(
            requirement_id=uuid4(), suite="test_nothing.py", acceptance_criteria=["AC1: works"]
        ),
    )
    await agent.handle_message(message, ctx)
    run = await TestRunStore(ctx.sessions).get(message.message_id)
    assert run.status == RunStatus.PENDING  # waiting for WARDEN, no FK violations
