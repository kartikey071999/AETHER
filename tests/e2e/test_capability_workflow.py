"""End-to-end capability workflow (spec section 22, phase 5) through the real runtime.

One engine runs every agent, the message bus, the scheduler and the security layer against a
temporary SQLite database. Models are a scripted mock (no network). Every step is asserted
from persisted records, never from in-memory state.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from reliability_support import QuietMonitor

from aether.agents.forge.schemas import ForgeConfig
from aether.communication.messages import AgentId, MessageType
from aether.models.contracts import CompletionRequest
from aether.runtime.scenario import (
    ACCEPTANCE_CRITERIA,
    MODULE_FILE,
    TEST_FILE,
    CapabilityScenario,
    ScenarioTimeout,
    ScenarioTrace,
    ScriptedCapabilityProvider,
    capability_finished,
    forge_operation_approved,
    format_trace,
    scenario_settings,
    verification_failed,
)
from aether.storage.models import TaskRecord

# Host CPU load must not defer or pause the workflow (the scheduler reads the resource monitor).
QUIET = QuietMonitor()

pytestmark = pytest.mark.slow

# Each path takes ~10-20s locally; the bound keeps a hung run from stalling the suite.
PATH_TIMEOUT = 50.0

Window = tuple[datetime, datetime | None]


def _report(trace: ScenarioTrace) -> str:
    return "\n".join(format_trace(trace))


async def _run(
    scenario: CapabilityScenario, predicate: Callable[[ScenarioTrace], bool], *, tag: str
) -> ScenarioTrace:
    objective_id = await scenario.submit_objective(tag=tag)
    try:
        return await scenario.run_until(objective_id, predicate, timeout=PATH_TIMEOUT)
    except ScenarioTimeout as error:
        pytest.fail(f"scenario did not finish:\n{_report(error.trace)}")


def _overlaps(first: Window, second: Window) -> bool:
    first_end = first[1] or datetime.max.replace(tzinfo=first[0].tzinfo)
    second_end = second[1] or datetime.max.replace(tzinfo=second[0].tzinfo)
    return first[0] < second_end and second[0] < first_end


def _titled(trace: ScenarioTrace, agent: AgentId, prefix: str) -> list[TaskRecord]:
    return [task for task in trace.tasks_for(agent) if task.title.startswith(prefix)]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _forge_requests(provider: ScriptedCapabilityProvider) -> list[CompletionRequest]:
    return [
        request
        for request in provider.requests
        if request.messages and request.messages[0].content.startswith("You are FORGE")
    ]


async def test_happy_path_verifies_the_capability_and_reports_it(tmp_path: Path) -> None:
    settings = scenario_settings(tmp_path)
    provider = ScriptedCapabilityProvider()
    async with CapabilityScenario(
        settings, model_providers=[provider], resource_monitor=QUIET
    ) as scenario:
        trace = await _run(scenario, capability_finished, tag="happy")
    report = _report(trace)
    # text_statistics is a known capability: FORGE used its template and never asked an LLM.
    assert _forge_requests(provider) == [], report

    # 1. The objective was decomposed into tasks with dependencies.
    assert trace.objective.completion_criteria == list(ACCEPTANCE_CRITERIA)
    for agent in (AgentId.SCOUT, AgentId.ROGUE, AgentId.WARDEN, AgentId.FORGE, AgentId.LEDGER):
        assert trace.tasks_for(agent), f"no task for {agent}\n{report}"
    [forge_task] = _titled(trace, AgentId.FORGE, "Implement")
    [consolidate] = _titled(trace, AgentId.SCOUT, "Consolidate")
    depends = {(dep.task_id, dep.depends_on_task_id) for dep in trace.dependencies}
    [scout_research] = _titled(trace, AgentId.SCOUT, "Research")
    [rogue_research] = _titled(trace, AgentId.ROGUE, "Explore")
    assert (consolidate.id, scout_research.id) in depends, report
    assert (consolidate.id, rogue_research.id) in depends, report
    assert any(task_id == forge_task.id for task_id, _ in depends), report

    # 2. research.request went to SCOUT and ROGUE and their research ran concurrently.
    assert trace.messages_of(MessageType.RESEARCH_REQUEST, target=AgentId.SCOUT), report
    assert trace.messages_of(MessageType.RESEARCH_REQUEST, target=AgentId.ROGUE), report
    scout_windows = [
        window
        for task in trace.tasks_for(AgentId.SCOUT)
        if task.id != consolidate.id and (window := trace.run_window(task.id))
    ]
    rogue_windows = [
        window for task in trace.tasks_for(AgentId.ROGUE) if (window := trace.run_window(task.id))
    ]
    assert any(_overlaps(s, r) for s in scout_windows for r in rogue_windows), report

    # 3. ROGUE's divergent finding reached SCOUT.
    assert trace.messages_of(
        MessageType.RESEARCH_FINDING, source=AgentId.ROGUE, target=AgentId.SCOUT
    ), report
    assert any(finding.agent == AgentId.ROGUE.value for finding in trace.findings), report

    # 4. SCOUT adopted one approach and rejected / asked for evidence on another, with reasons.
    adopted = [finding for finding in trace.findings if finding.status == "adopted"]
    others = [f for f in trace.findings if f.status in {"rejected", "needs_evidence"}]
    assert len(adopted) == 1 and adopted[0].rationale, report
    assert others and all(finding.rationale for finding in others), report
    [requirement] = trace.requirements
    assert requirement.created_by == AgentId.SCOUT.value
    assert requirement.acceptance_criteria == list(ACCEPTANCE_CRITERIA)

    # 5. LEDGER recorded the requirement and moved it through the workflow states.
    event_types = [event.event_type for event in trace.requirement_events]
    assert "requirement.created" in event_types, report
    assert event_types.count("requirement.status_changed") >= 2, report
    assert trace.messages_of(
        MessageType.REQUIREMENT_EVALUATED, source=AgentId.SCOUT, target=AgentId.LEDGER
    ), report
    assert requirement.status == "satisfied", report

    # 5b. ORCHESTR linked the requirement to the tasks that build and verify it (no guessing).
    [verify_task] = _titled(trace, AgentId.LEDGER, "Verify")
    assert forge_task.requirement_id == requirement.id, report
    assert verify_task.requirement_id == requirement.id, report
    assert trace.messages_of(
        MessageType.REQUIREMENT_EVALUATED, source=AgentId.SCOUT, target=AgentId.ORCHESTR
    ), report

    # 5c. Task timestamps come from the lease and the final transition, not from events.
    for task in (forge_task, verify_task):
        assert task.started_at is not None and task.completed_at is not None, report
        assert task.started_at <= task.completed_at, report
        assert task.state == "completed", report
    # The verification task finished only after LEDGER's real test run had finished.
    [finished_run] = trace.test_runs
    assert finished_run.finished_at is not None, report
    assert _aware(verify_task.completed_at) >= _aware(finished_run.finished_at), report

    # 6. WARDEN decided FORGE's operations, bound to the exact input hash.
    approved = {
        decision.id: decision
        for decision in trace.security_decisions
        if decision.decision == "approved" and decision.task_id == forge_task.id
    }
    assert approved and all(decision.input_hash for decision in approved.values()), report

    # 7. The scheduler recorded why it released each task.
    selected = {event.task_id for event in trace.scheduler_decisions}
    assert {forge_task.id, scout_research.id, rogue_research.id} <= selected, report

    # 8. FORGE wrote the module and its tests and recorded the execution.
    forge_runs = [run for run in trace.execution_runs if run.agent == AgentId.FORGE.value]
    assert forge_runs and all(run.status == "succeeded" for run in forge_runs), report
    assert all(run.security_decision_id in approved for run in forge_runs), report
    workspace = settings.workspace_dir / str(forge_task.id)
    assert (workspace / MODULE_FILE).is_file(), report
    assert (workspace / TEST_FILE).is_file(), report

    # 9. LEDGER ran the acceptance tests itself and recorded the results.
    [test_run] = trace.test_runs
    assert (test_run.passed, test_run.failed, test_run.errors) == (3, 0, 0), report
    assert TEST_FILE in test_run.suite
    assert len(trace.test_results) == 3
    assert {result.outcome for result in trace.test_results} == {"passed"}

    # 10. Verified only because every criterion passed.
    criteria = {item["criterion"]: item["passed"] for item in test_run.acceptance_results}
    assert criteria == dict.fromkeys(ACCEPTANCE_CRITERIA, True), report
    assert trace.verdicts == ["verified"], report
    # Research candidates are recorded as capabilities too; exactly one became verified.
    verified = [c for c in trace.capabilities if c.verification_status == "verified"]
    assert len(verified) == 1, report
    assert verified[0].requirement_id == requirement.id, report
    assert verified[0].lifecycle_status == "verified", report

    # 11. ORCHESTR decided to update the user and completed the objective.
    assert trace.decisions("user_update"), report
    assert trace.decisions("objective_completed"), report
    assert trace.completed

    # 12. MUSE wrote the update, separating verified results from uncertainty.
    [outbox] = trace.outbox_paths
    text = outbox.read_text(encoding="utf-8")
    verified_part, _, uncertain_part = text.partition("## Uncertain")
    assert "## Verified" in verified_part
    assert all(criterion in verified_part for criterion in ACCEPTANCE_CRITERIA)
    assert uncertain_part.strip()
    assert trace.interactions, report
    assert trace.messages_of(
        MessageType.COMMUNICATION_DELIVERED, source=AgentId.MUSE, target=AgentId.ORCHESTR
    ), report


async def test_buggy_module_fails_verification_and_is_never_verified(tmp_path: Path) -> None:
    provider = ScriptedCapabilityProvider(buggy=True)
    scenario = CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[provider],
        max_replans=0,
        resource_monitor=QUIET,
        # FORGE asks the LLM before its deterministic template, so the buggy draft is used.
        forge_config=ForgeConfig(llm_refine_templates=True),
    )
    async with scenario:
        trace = await _run(scenario, verification_failed, tag="buggy")
    report = _report(trace)
    assert len(_forge_requests(provider)) == 1, report

    [test_run] = trace.test_runs
    assert (test_run.passed, test_run.failed) == (1, 2), report
    failing = {item["criterion"] for item in test_run.acceptance_results if not item["passed"]}
    assert failing == set(ACCEPTANCE_CRITERIA[1:]), report
    assert "verified" not in trace.verdicts
    assert trace.capabilities, report
    assert all(c.verification_status != "verified" for c in trace.capabilities), report
    # Registration is blocked: nothing reached the verified lifecycle stage or was announced.
    assert all(c.lifecycle_status != "verified" for c in trace.capabilities), report
    assert not trace.messages_of(MessageType.CAPABILITY_REGISTER), report
    assert trace.messages_of(MessageType.BLOCKER, source=AgentId.LEDGER, target=AgentId.FORGE)
    assert not trace.completed
    assert not trace.decisions("objective_completed"), report
    # max_replans=0: ORCHESTR escalates rather than planning a resolution task it may not run.
    assert not [task for task in trace.tasks if "Resolve" in task.title], report


async def test_restart_mid_workflow_resumes_without_duplicates(tmp_path: Path) -> None:
    settings = scenario_settings(tmp_path)
    first = CapabilityScenario(
        settings, model_providers=[ScriptedCapabilityProvider()], resource_monitor=QUIET
    )
    async with first:
        objective_id = await first.submit_objective(tag="restart")
        try:
            before = await first.run_until(
                objective_id, forge_operation_approved, timeout=PATH_TIMEOUT
            )
        except ScenarioTimeout as error:
            pytest.fail(f"no WARDEN approval before the restart:\n{_report(error.trace)}")
    assert not before.completed
    assert not before.test_runs, "the restart should interrupt the workflow before verification"

    second = CapabilityScenario(
        settings,
        model_providers=[ScriptedCapabilityProvider()],
        create_schema=False,
        resource_monitor=QUIET,
    )
    async with second:
        try:
            trace = await second.run_until(objective_id, capability_finished, timeout=PATH_TIMEOUT)
        except ScenarioTimeout as error:
            pytest.fail(f"workflow did not resume after the restart:\n{_report(error.trace)}")
    report = _report(trace)

    assert len(trace.requirements) == 1, report
    assert len([f for f in trace.findings if f.status == "adopted"]) == 1, report
    forge_runs = [run for run in trace.execution_runs if run.agent == AgentId.FORGE.value]
    assert len(forge_runs) == 1, report
    assert len(trace.test_runs) == 1, report
    verified = [c for c in trace.capabilities if c.verification_status == "verified"]
    assert len(verified) == 1, report
    assert trace.verdicts == ["verified"], report
    assert len(trace.messages_of(MessageType.VERIFICATION_REQUEST, source=AgentId.FORGE)) == 1
    assert len(trace.outbox_paths) == 1, report
    assert len(trace.decisions("objective_completed")) == 1, report


async def test_resubmitting_the_same_tag_tracks_the_new_objective(tmp_path: Path) -> None:
    # Regression: the demo reused one tag, so the trace followed an older completed objective
    # and reported success for a run that never executed.
    async with CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[ScriptedCapabilityProvider()],
        resource_monitor=QUIET,
    ) as scenario:
        first = await scenario.submit_objective(tag="same")
        second = await scenario.submit_objective(tag="same")
    assert first != second
