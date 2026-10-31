"""Blocker routing, escalation, authorization questions, and user responses."""

from uuid import uuid4

from orchestr_helpers import Harness

from aether.agents.orchestr.blockers import _base_title
from aether.agents.orchestr.limits import OrchestrLimits
from aether.communication.contracts import (
    AuthorizationRequestPayload,
    BlockerKind,
    BlockerPayload,
    TaskOutcome,
    TaskResultPayload,
    UserDecision,
    UserResponsePayload,
)
from aether.communication.messages import AgentId, MessageType
from aether.storage.models import TaskStatus


async def _ready_task(harness: Harness):
    objective_id = await harness.objective()
    tasks = await harness.tasks(objective_id)
    return objective_id, tasks["research.scout"], tasks


def _blocker(task, kind=BlockerKind.RESEARCH, route_to=AgentId.SCOUT, **extra) -> BlockerPayload:
    return BlockerPayload(
        task_id=task.id,
        objective_id=task.objective_id,
        kind=kind,
        description="cannot find a maintained library",
        route_to=route_to,
        **extra,
    )


async def test_research_blocker_creates_resolution_task_and_dependency(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)

    assert await harness.state(task.id) is TaskStatus.BLOCKED
    resolutions = [
        item
        for item in await harness.store.tasks_of(objective_id)
        if item.parent_task_id == task.id
    ]
    assert len(resolutions) == 1
    assert resolutions[0].assigned_agent == AgentId.SCOUT.value
    deps = await harness.store.dependencies_of([task.id])
    assert deps[task.id] == {resolutions[0].id}


async def test_blocked_task_resumes_when_resolution_completes(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)
    resolution = next(
        item
        for item in await harness.store.tasks_of(objective_id)
        if item.parent_task_id == task.id
    )
    await harness.tick()
    assert await harness.state(resolution.id) is TaskStatus.READY
    await harness.finish(resolution.id)
    assert await harness.state(task.id) in (TaskStatus.PENDING, TaskStatus.READY)


async def test_no_resolution_task_is_created_when_replans_are_disabled(harness: Harness) -> None:
    harness.use_limits(OrchestrLimits(llm_refinement=False, max_replans_per_objective=0))
    objective_id, task, _ = await _ready_task(harness)
    before = {item.id for item in await harness.store.tasks_of(objective_id)}

    await harness.send(
        AgentId.FORGE,
        _blocker(task, BlockerKind.IMPLEMENTATION, AgentId.FORGE),
        task_id=task.id,
    )

    after = await harness.store.tasks_of(objective_id)
    assert {item.id for item in after} == before
    assert not [item for item in after if "Resolve" in item.title]
    assert await harness.state(task.id) is TaskStatus.BLOCKED
    assert (await harness.get_objective(objective_id)).status == "blocked"
    escalations = await harness.store.keyed_decisions("orchestr:escalated:")
    assert len(escalations) == 1
    assert "replanning is disabled" in escalations[0].rationale
    assert harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)


async def test_blocker_redelivery_creates_one_resolution(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    payload = _blocker(task)
    await harness.send(AgentId.SCOUT, payload, task_id=task.id)
    await harness.send(AgentId.SCOUT, payload, task_id=task.id)
    resolutions = [
        item
        for item in await harness.store.tasks_of(objective_id)
        if item.parent_task_id == task.id
    ]
    assert len(resolutions) == 1


async def test_repeated_blockers_escalate_to_the_user_once(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    await harness.send(AgentId.SCOUT, _blocker(task, attempts=3), task_id=task.id)
    await harness.send(AgentId.SCOUT, _blocker(task, attempts=4), task_id=task.id)

    assert (await harness.get_objective(objective_id)).status == "blocked"
    assert await harness.state(task.id) is TaskStatus.BLOCKED
    messages = harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
    assert len(messages) == 1  # same objective + blocker kind within the window: one escalation
    keyed = await harness.store.keyed_decisions("orchestr:escalated:")
    assert len(keyed) == 1
    assert len(await harness.store.keyed_decisions("orchestr:aggregated:")) == 1


async def test_same_blocker_redelivered_escalates_once(harness: Harness) -> None:
    _, task, _ = await _ready_task(harness)
    payload = _blocker(task, attempts=3)
    await harness.send(AgentId.SCOUT, payload, task_id=task.id)
    await harness.send(AgentId.SCOUT, payload, task_id=task.id)
    assert len(harness.bus.of_type(MessageType.COMMUNICATION_REQUEST)) == 1


async def test_authorization_blocker_asks_the_user(harness: Harness) -> None:
    _, task, _ = await _ready_task(harness)
    payload = _blocker(task, BlockerKind.AUTHORIZATION, AgentId.MUSE)
    await harness.send(AgentId.SCOUT, payload, task_id=task.id)

    assert await harness.state(task.id) is TaskStatus.BLOCKED
    pending = await harness.store.pending_authorization_for(task.id)
    assert pending is not None
    assert len(harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)) == 1


async def _authorization(harness: Harness):
    objective_id, task, _ = await _ready_task(harness)
    await harness.send(
        AgentId.SCOUT, _blocker(task, BlockerKind.AUTHORIZATION, AgentId.MUSE), task_id=task.id
    )
    pending = await harness.store.pending_authorization_for(task.id)
    assert pending is not None
    return objective_id, task, pending


async def test_user_approval_unblocks_the_task(harness: Harness) -> None:
    _, task, pending = await _authorization(harness)
    await harness.send(
        AgentId.MUSE,
        UserResponsePayload(
            authorization_request_id=pending.id, decision=UserDecision.APPROVED, text="yes"
        ),
    )
    assert await harness.state(task.id) in (TaskStatus.PENDING, TaskStatus.READY)


async def test_user_denial_cancels_task_and_blocks_objective(harness: Harness) -> None:
    objective_id, task, pending = await _authorization(harness)
    await harness.send(
        AgentId.MUSE,
        UserResponsePayload(authorization_request_id=pending.id, decision=UserDecision.DENIED),
    )
    assert await harness.state(task.id) is TaskStatus.CANCELLED
    assert (await harness.get_objective(objective_id)).status == "blocked"


async def test_reply_without_decision_changes_nothing(harness: Harness) -> None:
    _, task, pending = await _authorization(harness)
    await harness.send(
        AgentId.MUSE,
        UserResponsePayload(
            authorization_request_id=pending.id,
            decision=UserDecision.NONE,
            text="maybe, tell me more",
        ),
    )
    assert await harness.state(task.id) is TaskStatus.BLOCKED
    still_pending = await harness.store.pending_authorization_for(task.id)
    assert still_pending is not None


async def test_decision_for_unknown_request_is_ignored(harness: Harness) -> None:
    _, task, _ = await _ready_task(harness)
    await harness.send(
        AgentId.MUSE,
        UserResponsePayload(authorization_request_id=uuid4(), decision=UserDecision.APPROVED),
    )
    assert await harness.state(task.id) is TaskStatus.READY


async def test_warden_authorization_request_is_forwarded_to_the_user(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    request_id = uuid4()
    await harness.send(
        AgentId.WARDEN,
        AuthorizationRequestPayload(
            authorization_request_id=request_id,
            question="Allow network access to pypi.org?",
            operation_summary="pip download",
            objective_id=objective_id,
            task_id=task.id,
        ),
    )
    forwarded = harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
    assert len(forwarded) == 1
    assert await harness.state(task.id) is TaskStatus.BLOCKED


async def test_blocked_result_is_treated_as_a_blocker(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    await harness.run_to(task.id, TaskStatus.BLOCKED)
    message = harness.result_message(
        task,
        TaskResultPayload(
            task_id=task.id, status=TaskOutcome.BLOCKED, error="no sources reachable"
        ),
    )
    await harness.agent.dispatch_message(message, harness.ctx)
    resolutions = [
        item
        for item in await harness.store.tasks_of(objective_id)
        if item.parent_task_id == task.id
    ]
    assert len(resolutions) == 1


async def test_unretryable_failure_escalates(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    message = harness.result_message(
        task,
        TaskResultPayload(
            task_id=task.id, status=TaskOutcome.FAILED, error="corrupt input", retryable=False
        ),
    )
    await harness.agent.dispatch_message(message, harness.ctx)
    assert (await harness.get_objective(objective_id)).status == "blocked"


async def test_task_completion_does_not_message_the_user(harness: Harness) -> None:
    _, task, _ = await _ready_task(harness)
    await harness.finish(task.id)
    message = harness.result_message(
        task, TaskResultPayload(task_id=task.id, status=TaskOutcome.COMPLETED, summary="done")
    )
    await harness.agent.dispatch_message(message, harness.ctx)
    assert harness.bus.of_type(MessageType.COMMUNICATION_REQUEST) == []


def _forge_blocker(task, **extra) -> BlockerPayload:
    return _blocker(task, BlockerKind.IMPLEMENTATION, AgentId.FORGE, **extra)


async def _resolutions(harness: Harness, objective_id) -> list:
    return [item for item in await harness.store.tasks_of(objective_id) if "Resolve" in item.title]


async def test_blocked_resolution_task_escalates_instead_of_nesting(harness: Harness) -> None:
    objective_id, task, _ = await _ready_task(harness)
    await harness.send(AgentId.FORGE, _forge_blocker(task), task_id=task.id)
    [resolution] = await _resolutions(harness, objective_id)
    assert resolution.title.startswith("Resolve implementation blocker: ")

    await harness.send(AgentId.FORGE, _forge_blocker(resolution), task_id=resolution.id)

    assert [item.id for item in await _resolutions(harness, objective_id)] == [resolution.id]
    assert (await harness.get_objective(objective_id)).status == "blocked"
    [escalation] = await harness.store.keyed_decisions("orchestr:escalated:")
    assert "resolution task is itself blocked" in escalation.rationale
    assert harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)


async def test_blocker_resolutions_count_against_the_replan_limit(harness: Harness) -> None:
    harness.use_limits(OrchestrLimits(llm_refinement=False, max_replans_per_objective=1))
    objective_id, first, tasks = await _ready_task(harness)
    second = tasks["research.rogue"]

    await harness.send(AgentId.SCOUT, _blocker(first), task_id=first.id)
    await harness.send(AgentId.ROGUE, _blocker(second, route_to=AgentId.ROGUE), task_id=second.id)

    assert len(await _resolutions(harness, objective_id)) == 1
    replans = await harness.store.decisions(objective_id=objective_id, decision="replan")
    assert len(replans) == 1
    assert (await harness.get_objective(objective_id)).status == "blocked"
    [escalation] = await harness.store.keyed_decisions("orchestr:escalated:")
    assert "replanning limit of 1 reached" in escalation.rationale


async def test_blocker_resolution_respects_the_objective_task_limit(harness: Harness) -> None:
    objective_id, task, tasks = await _ready_task(harness)
    harness.use_limits(OrchestrLimits(llm_refinement=False, max_tasks_per_objective=len(tasks)))

    await harness.send(AgentId.SCOUT, _blocker(task), task_id=task.id)

    assert await _resolutions(harness, objective_id) == []
    assert len(await harness.store.tasks_of(objective_id)) == len(tasks)
    [escalation] = await harness.store.keyed_decisions("orchestr:escalated:")
    assert "task limit" in escalation.rationale


def test_resolution_titles_never_nest() -> None:
    nested = "Resolve implementation blocker: Resolve research blocker: Implement X"
    assert _base_title(nested) == "Implement X"
    assert _base_title("Implement X") == "Implement X"


async def test_always_failing_forge_is_bounded_and_escalated(harness: Harness) -> None:
    """Regression for the runaway: every FORGE task (including resolutions) reports blocked."""
    objective_id = await harness.objective()
    initial = len(await harness.store.tasks_of(objective_id))
    for _ in range(50):
        if (await harness.get_objective(objective_id)).status != "active":
            break
        candidates = [
            item
            for item in await harness.store.tasks_of(objective_id)
            if item.assigned_agent == AgentId.FORGE.value
            and TaskStatus(item.state) not in (TaskStatus.BLOCKED, TaskStatus.CANCELLED)
        ]
        assert candidates, "FORGE work should remain until the objective is escalated"
        for item in candidates:
            message = harness.result_message(
                item,
                TaskResultPayload(
                    task_id=item.id, status=TaskOutcome.BLOCKED, error="no implementation plan"
                ),
            )
            await harness.agent.dispatch_message(message, harness.ctx)

    tasks = await harness.store.tasks_of(objective_id)
    limits = OrchestrLimits()
    assert len(tasks) <= initial + limits.max_replans_per_objective
    assert not [item for item in tasks if item.title.count("blocker:") > 1]
    assert (await harness.get_objective(objective_id)).status == "blocked"
    assert harness.bus.of_type(MessageType.COMMUNICATION_REQUEST, target=AgentId.MUSE)
