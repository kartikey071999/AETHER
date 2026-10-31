"""FORGE links its work to the structured requirement and names a real suite (T-040)."""

import json
import logging

import pytest
from forge_support import ForgeEnv

from aether.agents.base import TaskResultStatus
from aether.agents.forge.resolve import description_criteria, load_requirement, resolve_plan
from aether.communication.messages import MessageType
from aether.storage.models import RequirementStatus, TaskRecord
from aether.storage.repositories import RequirementRepository

OBJECTIVE_CRITERIA = ["AC1: counts words", "AC2: handles empty input"]

PLAN_WITH_TESTS = {
    "steps": [
        {
            "tool": "write_file",
            "arguments": {"path": "stats.py", "content": "def words(t):\n    return 0\n"},
            "purpose": "Write the module",
        },
        {
            "tool": "write_file",
            "arguments": {"path": "tests/test_stats.py", "content": "def test_ac1():\n    pass\n"},
            "purpose": "Write the acceptance tests",
        },
    ],
    "rollback_steps": [],
}


async def _objective_requirement(
    env: ForgeEnv, task: TaskRecord, status: RequirementStatus = RequirementStatus.APPROVED
):
    requirements = RequirementRepository(env.database.sessions)
    requirement_id = await requirements.create(
        objective_id=task.objective_id,
        title="Evaluate stats",
        requirement_type="capability",
        created_by="SCOUT",
        acceptance_criteria=list(OBJECTIVE_CRITERIA),
    )
    if status is not RequirementStatus.PROPOSED:
        await requirements.set_status(requirement_id, status, actor="LEDGER")
    return requirement_id


async def test_unlinked_task_uses_the_objectives_approved_requirement(env: ForgeEnv) -> None:
    # Regression: ORCHESTR plans FORGE's task before research adopts an approach, so the task
    # has no requirement_id; FORGE previously never requested verification for it.
    task = await env.make_task(with_requirement=False, criteria=None)
    requirement_id = await _objective_requirement(env, task)
    env.llm_replies.append(json.dumps(PLAN_WITH_TESTS))

    result = await env.agent.execute_task(task, env.ctx)

    assert result.status is TaskResultStatus.COMPLETED, result
    [execution] = await env.messages(MessageType.EXECUTION_RESULT)
    assert execution.payload["outputs"]["verification_requested"] is True
    [request] = await env.messages(MessageType.VERIFICATION_REQUEST)
    assert request.payload["requirement_id"] == str(requirement_id)
    assert request.payload["acceptance_criteria"] == OBJECTIVE_CRITERIA
    # The suite is the test file the plan wrote, runnable in the task workspace.
    assert request.payload["suite"] == "tests/test_stats.py"
    assert {"workspace": str(task.id)} in request.payload["evidence"]


async def test_legacy_fallback_is_logged_and_linked_tasks_do_not_use_it(
    env: ForgeEnv, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = await env.make_task(with_requirement=False, criteria=None)
    requirement_id = await _objective_requirement(env, legacy)
    with caplog.at_level(logging.WARNING, logger="aether.agent"):
        requirement = await load_requirement(env.ctx, legacy)
    assert requirement is not None and requirement.id == requirement_id
    assert "forge.legacy_requirement_fallback" in caplog.text

    caplog.clear()
    linked = await env.make_task()
    with caplog.at_level(logging.WARNING, logger="aether.agent"):
        assert await load_requirement(env.ctx, linked) is not None
    assert "forge.legacy_requirement_fallback" not in caplog.text


async def test_proposed_requirement_is_not_picked_up(env: ForgeEnv) -> None:
    task = await env.make_task(with_requirement=False, criteria=None)
    await _objective_requirement(env, task, RequirementStatus.PROPOSED)

    assert await load_requirement(env.ctx, task) is None


async def test_requirement_criteria_win_over_the_assignment_hint(env: ForgeEnv) -> None:
    task = await env.make_task(criteria=OBJECTIVE_CRITERIA)
    env.llm_replies.append(json.dumps(PLAN_WITH_TESTS))

    plan, refusal = await resolve_plan(
        env.ctx, task, env.config, criteria_hint=["Findings submitted with evidence"]
    )

    assert refusal is None and plan is not None
    assert list(plan.acceptance_criteria) == OBJECTIVE_CRITERIA


async def test_description_criteria_are_the_last_fallback(env: ForgeEnv) -> None:
    task = await env.make_task(
        with_requirement=False,
        criteria=None,
        description="Implement stats\n\nAcceptance criteria:\n- AC1: counts words\n",
    )
    env.llm_replies.append(json.dumps(PLAN_WITH_TESTS))

    plan, refusal = await resolve_plan(env.ctx, task, env.config)

    assert refusal is None and plan is not None
    assert list(plan.acceptance_criteria) == ["AC1: counts words"]
    assert plan.requirement_id is None


def test_description_criteria_parses_the_bullets_under_the_heading() -> None:
    text = (
        "Intro\n\nAcceptance criteria:\n- AC1: one\n* AC2: two\n\nNotes follow\n- not a criterion"
    )
    assert description_criteria(text) == ("AC1: one", "AC2: two")
    assert description_criteria("no heading\n- bullet") == ()
