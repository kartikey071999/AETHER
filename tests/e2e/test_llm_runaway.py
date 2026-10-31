"""Offline reproduction of the LLM cost runaway (T-045) through the real runtime.

The live incident: FORGE's plan drafts were cut off at ``max_tokens`` by reasoning models, the
router fell back across all three models for every attempt, and each failure became a new
nested blocker-resolution task: 198 tasks and ~2M tokens for one trivial objective. Here three
mock models reproduce the truncation behind the real router, budget guard and governor; the
workflow must stay within the per-objective FORGE budget and a bounded number of tasks.
"""

from pathlib import Path

import pytest
from reliability_support import QuietMonitor
from sqlalchemy import select

from aether.agents.forge.schemas import ForgeConfig
from aether.communication.messages import AgentId
from aether.models import CompletionRequest, CompletionResponse, ModelInfo, ModelTier
from aether.models.settings import ProviderSettings
from aether.runtime.scenario import (
    CapabilityScenario,
    ScenarioTimeout,
    ScenarioTrace,
    ScriptedCapabilityProvider,
    capability_finished,
    format_trace,
    scenario_settings,
)
from aether.storage.models import EventRecord

PATH_TIMEOUT = 50.0
MODELS = ("gpt-oss-20b", "gpt-oss-120b", "qwen-27b")
TRUNCATED_PLAN = '{"steps": [{"tool": "write_file", "arguments": {"path": "text_statistics.py", "co'
FORGE_BUDGET = ProviderSettings(_env_file=None).agent_budget(AgentId.FORGE.value)


class TruncatingForgeProvider(ScriptedCapabilityProvider):
    """Three models; FORGE plan drafts always hit the output limit with invalid JSON."""

    def __init__(self) -> None:
        super().__init__()
        tiers = frozenset({ModelTier.LIGHT, ModelTier.STANDARD, ModelTier.REASONING})
        self._models = [ModelInfo(name=name, tiers=tiers) for name in MODELS]
        self.forge_calls: list[CompletionResponse] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.messages and request.messages[0].content.startswith("You are FORGE"):
            self.requests.append(request)
            response = CompletionResponse(
                text=TRUNCATED_PLAN,
                provider=self.name,
                model=request.model or MODELS[0],
                input_tokens=min(request.input_token_estimate(), 2000),
                output_tokens=request.max_tokens,
                latency_ms=1,
                finish_reason="length",
            )
            self.forge_calls.append(response)
            return response
        return await super().complete(request)


def _report(trace: ScenarioTrace) -> str:
    return "\n".join(format_trace(trace))


def _escalated(trace: ScenarioTrace) -> bool:
    return trace.objective.status == "blocked" and bool(trace.decisions("escalated"))


async def _run(scenario: CapabilityScenario, predicate, *, tag: str) -> ScenarioTrace:
    objective_id = await scenario.submit_objective(tag=tag)
    try:
        return await scenario.run_until(objective_id, predicate, timeout=PATH_TIMEOUT)
    except ScenarioTimeout as error:
        pytest.fail(f"scenario did not finish:\n{_report(error.trace)}")


async def _events(scenario: CapabilityScenario, event_type: str) -> list[EventRecord]:
    assert scenario.runtime is not None
    async with scenario.runtime.database.sessions() as session:
        statement = select(EventRecord).where(EventRecord.event_type == event_type)
        return list(await session.scalars(statement))


def _assert_forge_spend_is_bounded(provider: TruncatingForgeProvider, report: str) -> None:
    calls = provider.forge_calls
    tokens = sum(call.input_tokens + call.output_tokens for call in calls)
    assert FORGE_BUDGET.max_requests_per_objective is not None
    assert FORGE_BUDGET.max_tokens_per_objective is not None
    assert len(calls) <= FORGE_BUDGET.max_requests_per_objective, report
    assert tokens <= FORGE_BUDGET.max_tokens_per_objective, report
    # At most two models per request (no cascade to the third), each retried once on length.
    assert {call.model for call in calls} <= set(MODELS[:2]), report
    assert len(calls) <= 4, report


async def test_truncated_forge_drafts_fall_back_to_the_template_and_finish(
    tmp_path: Path,
) -> None:
    provider = TruncatingForgeProvider()
    scenario = CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[provider],
        # Host-load independent: these tests are about LLM bounds, not resource admission.
        resource_monitor=QuietMonitor(),
        # As in the incident: FORGE asks the LLM before using anything deterministic.
        forge_config=ForgeConfig(llm_refine_templates=True),
    )
    async with scenario:
        trace = await _run(scenario, capability_finished, tag="runaway-template")
    report = _report(trace)

    assert provider.forge_calls, "FORGE should have tried the LLM once"
    _assert_forge_spend_is_bounded(provider, report)
    assert trace.verdicts == ["verified"], report
    assert not [task for task in trace.tasks if "Resolve" in task.title], report


async def test_truncated_forge_drafts_without_a_template_escalate_with_bounded_tasks(
    tmp_path: Path,
) -> None:
    provider = TruncatingForgeProvider()
    scenario = CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[provider],
        # Host-load independent: these tests are about LLM bounds, not resource admission.
        resource_monitor=QuietMonitor(),
        forge_config=ForgeConfig(capability_templates=False),
    )
    async with scenario:
        trace = await _run(scenario, _escalated, tag="runaway-escalate")
        exhausted = await _events(scenario, "provider.budget_exhausted")
    report = _report(trace)

    _assert_forge_spend_is_bounded(provider, report)
    # One draft (for the first FORGE task); later FORGE tasks found the budget exhausted.
    forge_task_ids = {task.id for task in trace.tasks_for(AgentId.FORGE)}
    drafted = {
        request.task_id
        for request in provider.requests
        if request.messages[0].content.startswith("You are FORGE")
    }
    assert len(drafted) == 1 and drafted <= forge_task_ids, report
    assert [event.payload["agent"] for event in exhausted] == [AgentId.FORGE.value], report
    resolutions = [task for task in trace.tasks if task.title.startswith("Resolve")]
    assert len(resolutions) <= 2, report  # max_replans_per_objective
    assert len(trace.tasks) < 20, report
    assert not [task for task in trace.tasks if task.title.count("blocker:") > 1], report
    assert not trace.completed, report
