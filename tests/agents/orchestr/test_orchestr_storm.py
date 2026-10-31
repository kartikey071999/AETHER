"""Notification-storm regressions (T-047): terminal objectives, escalation dedupe, stalled waits."""

from uuid import UUID, uuid4

from orchestr_helpers import Harness

from aether.agents.orchestr.blockers import escalate_objective
from aether.agents.orchestr.env import build_env
from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import (
    AcceptanceResult,
    BlockerKind,
    BlockerPayload,
    TaskOutcome,
    TaskResultPayload,
    TestCounts,
    TestReportPayload,
    VerificationVerdict,
)
from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus


def _blocker(task, kind=BlockerKind.RESEARCH, route_to=AgentId.SCOUT) -> BlockerPayload:
    return BlockerPayload(
        task_id=task.id,
        objective_id=task.objective_id,
        kind=kind,
        description="cannot find a maintained library",
        route_to=route_to,
        attempts=3,
    )


def _muse(harness: Harness) -> list:
    return harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)


async def _ignored(harness: Harness, objective_id: UUID) -> list:
    return await harness.store.decisions(
        objective_id=objective_id, decision="ignored_terminal_objective"
    )


async def test_blocker_storm_for_a_cancelled_objective_is_only_recorded(harness: Harness) -> None:
    objective_id = await harness.objective()
    task = (await harness.tasks(objective_id))["research.scout"]
    await harness.store.objectives.set_status(
        objective_id, "cancelled", actor="MUSE", reason="user cancelled"
    )
    before = {item.id for item in await harness.store.tasks_of(objective_id)}

    for _ in range(55):
        await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    await harness.tick()

    assert _muse(harness) == []
    assert await harness.store.keyed_decisions("orchestr:escalated:") == []
    assert await harness.store.decisions(objective_id=objective_id, decision="replan") == []
    assert {item.id for item in await harness.store.tasks_of(objective_id)} == before
    assert len(await _ignored(harness, objective_id)) == 55
    assert (await harness.get_objective(objective_id)).status == "cancelled"


async def test_results_and_reports_for_finished_objectives_are_ignored(harness: Harness) -> None:
    objective_id = await harness.objective()
    task = (await harness.tasks(objective_id))["research.scout"]
    await harness.store.objectives.set_status(
        objective_id, "failed", actor="ORCHESTR", reason="gave up"
    )
    result = TaskResultPayload(
        task_id=task.id, status=TaskOutcome.FAILED, retryable=False, error="boom"
    )
    await harness.agent.dispatch_message(harness.result_message(task, result), harness.ctx)
    await harness.send(
        AgentId.LEDGER,
        TestReportPayload(
            test_run_id=uuid4(),
            task_id=task.id,
            suite="acceptance",
            results=TestCounts(passed=1, failed=2),
            acceptance_results=[AcceptanceResult(criterion="x", passed=False, evidence="ran")],
            verdict=VerificationVerdict.FAILED,
        ),
        objective_id=objective_id,
    )

    assert _muse(harness) == []
    assert await harness.store.keyed_decisions("orchestr:escalated:") == []
    ignored = await _ignored(harness, objective_id)
    assert {view.evidence[0] for view in ignored} == {"task.result", "test.report"}


async def test_escalate_objective_never_escalates_a_terminal_objective(harness: Harness) -> None:
    objective_id = await harness.objective()
    await harness.store.objectives.set_status(
        objective_id, "completed", actor="ORCHESTR", reason="done"
    )
    env = build_env(harness.ctx)
    objective = await harness.get_objective(objective_id)
    raised = await escalate_objective(
        env, objective, key="direct", reason="late failure", kind="task_failure"
    )
    assert raised is False
    assert _muse(harness) == []
    assert (await harness.get_objective(objective_id)).status == "completed"


async def test_one_escalation_per_objective_and_kind_per_window(harness: Harness) -> None:
    objective_id = await harness.objective()
    task = (await harness.tasks(objective_id))["research.scout"]

    for _ in range(5):
        await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    assert len(_muse(harness)) == 1
    assert len(await harness.store.keyed_decisions("orchestr:escalated:")) == 1
    assert len(await harness.store.keyed_decisions("orchestr:aggregated:")) == 4

    # A different blocker kind is a different escalation.
    await harness.send(
        AgentId.FORGE,
        _blocker(task, BlockerKind.IMPLEMENTATION, AgentId.FORGE),
        task_id=task.id,
    )
    assert len(_muse(harness)) == 2

    # Inside the window more research blockers aggregate; after it a new escalation is allowed.
    harness.clock.advance(minutes=59)
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    assert len(_muse(harness)) == 2
    harness.clock.advance(minutes=2)
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    assert len(_muse(harness)) == 3
    assert len(await harness.store.keyed_decisions("orchestr:escalated:")) == 3


async def test_escalation_window_is_configurable(harness: Harness) -> None:
    harness.use_limits(OrchestrLimits(llm_refinement=False, escalation_window_hours=0.1))
    objective_id = await harness.objective()
    task = (await harness.tasks(objective_id))["research.scout"]
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    harness.clock.advance(minutes=7)
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    assert len(_muse(harness)) == 2


async def _reach_implementation(harness: Harness, objective_id: UUID) -> dict:
    tasks = await harness.tasks(objective_id)
    for key in ("research.scout", "research.rogue", "consolidation", "security_review"):
        await harness.finish(tasks[key].id)
    return await harness.tasks(objective_id)


async def test_forge_waiting_for_a_requirement_escalates_once_after_the_bound(
    harness: Harness,
) -> None:
    objective_id = await harness.objective()
    tasks = await _reach_implementation(harness, objective_id)
    implementation = tasks["implementation"].id
    assert await harness.state(implementation) is TaskStatus.PENDING
    assert await harness.store.keyed_decisions("orchestr:requirement-wait:")

    harness.clock.advance(minutes=9)
    await harness.tick()
    assert _muse(harness) == []
    assert (await harness.get_objective(objective_id)).status == "active"

    harness.clock.advance(minutes=2)
    await harness.tick()
    for _ in range(5):
        harness.clock.advance(minutes=30)
        await harness.tick()

    [escalation] = await harness.store.keyed_decisions("orchestr:escalated:")
    assert escalation.evidence[0] == "requirement"
    assert "approved requirement" in escalation.rationale
    assert len(_muse(harness)) == 1
    assert (await harness.get_objective(objective_id)).status == "blocked"
    assert await harness.state(implementation) is TaskStatus.PENDING


async def test_requirement_wait_bound_is_configurable(harness: Harness) -> None:
    harness.use_limits(OrchestrLimits(llm_refinement=False, requirement_wait_minutes=0.5))
    objective_id = await harness.objective()
    await _reach_implementation(harness, objective_id)
    harness.clock.advance(seconds=31)
    await harness.tick()
    assert len(_muse(harness)) == 1
