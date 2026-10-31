"""All providers over budget (T-047): the capability completes via fallbacks or escalates once.

The live incident ran with the Groq daily budget already exhausted: research ran on
deterministic fallbacks, SCOUT adopted nothing, no requirement was created and FORGE stayed
pending until the run timed out. Here every model call raises ``BudgetExceeded`` behind the real
router; the run must still finish, never loop and never spam the user.
"""

import asyncio
from pathlib import Path

import pytest

from aether.agents.forge.schemas import ForgeConfig
from aether.agents.scout import workflow as scout_workflow
from aether.communication.messages import AgentId, MessageType
from aether.models import CompletionRequest, CompletionResponse
from aether.models.budgets import BudgetExceeded
from aether.models.providers import MockProvider
from aether.observability.recorder import split_legacy_payload
from aether.runtime.resource_monitor import HostResources, ResourceMonitor
from aether.runtime.scenario import (
    CapabilityScenario,
    ScenarioTimeout,
    ScenarioTiming,
    ScenarioTrace,
    capability_finished,
    collect_trace,
    format_trace,
    scenario_settings,
)

PATH_TIMEOUT = 60.0


@pytest.fixture(autouse=True)
def idle_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """These scenarios test budget fallbacks, not host pressure: report an idle host.

    On a busy shared machine the real monitor can defer FORGE (CPU/RAM estimate) for longer
    than the scenario timeout, which would be indistinguishable from the stall under test.
    """
    monkeypatch.setattr(
        ResourceMonitor,
        "snapshot",
        lambda self, disk_path: HostResources(
            cpu_percent=5.0, available_ram_bytes=64 * 1024**3, free_disk_bytes=500 * 1024**3
        ),
    )


class ExhaustedProvider(MockProvider):
    """Every request is refused as over budget, like a provider whose daily quota is spent."""

    def __init__(self) -> None:
        super().__init__(name="mock")
        self.refused = 0

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.refused += 1
        raise BudgetExceeded("mock: exceeded daily request budget")


def _report(trace: ScenarioTrace) -> str:
    return "\n".join(format_trace(trace))


def _escalated(trace: ScenarioTrace) -> bool:
    return bool(trace.decisions("escalated"))


def _finished_or_escalated(trace: ScenarioTrace) -> bool:
    return capability_finished(trace) or _escalated(trace)


async def _run(scenario: CapabilityScenario, predicate, *, tag: str) -> ScenarioTrace:
    objective_id = await scenario.submit_objective(tag=tag)
    try:
        await scenario.run_until(objective_id, predicate, timeout=PATH_TIMEOUT)
    except ScenarioTimeout as error:
        pytest.fail(f"scenario stalled:\n{_report(error.trace)}")
    # Let queued work drain: nothing may keep escalating or notifying afterwards.
    await asyncio.sleep(2.0)
    assert scenario.runtime is not None
    return await collect_trace(scenario.runtime, objective_id)


def _assert_no_storm(trace: ScenarioTrace, report: str) -> None:
    assert len(trace.decisions("escalated")) <= 1, report
    assert len(trace.interactions) <= 2, report
    delivered = trace.messages_of(MessageType.COMMUNICATION_DELIVERED, source=AgentId.MUSE)
    assert len(delivered) <= 2 * len(trace.interactions), report
    assert len(trace.tasks) < 20, report
    assert not [task for task in trace.tasks if task.title.count("blocker:") > 1], report


async def test_all_providers_over_budget_complete_the_capability_via_fallbacks(
    tmp_path: Path,
) -> None:
    provider = ExhaustedProvider()
    scenario = CapabilityScenario(scenario_settings(tmp_path), model_providers=[provider])
    async with scenario:
        trace = await _run(scenario, _finished_or_escalated, tag="exhausted")
    report = _report(trace)

    assert provider.refused, "agents should have asked a model before falling back"
    # SCOUT adopted the conventional approach without a model, so FORGE was never left waiting.
    assert trace.requirements, report
    assert any(finding.status == "adopted" for finding in trace.findings), report
    assert trace.completed, report
    assert trace.verdicts == ["verified"], report
    assert not trace.decisions("escalated"), report
    _assert_no_storm(trace, report)


async def test_all_providers_over_budget_without_a_template_escalate_exactly_once(
    tmp_path: Path,
) -> None:
    provider = ExhaustedProvider()
    scenario = CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[provider],
        forge_config=ForgeConfig(capability_templates=False),
    )
    async with scenario:
        trace = await _run(scenario, _escalated, tag="exhausted-escalate")
    report = _report(trace)

    assert len(trace.decisions("escalated")) == 1, report
    assert not trace.completed, report
    _assert_no_storm(trace, report)


async def test_nothing_adoptable_escalates_once_within_the_requirement_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SCOUT's no-model path cannot adopt anything: FORGE must not stay pending forever.
    monkeypatch.setattr(scout_workflow, "conventional_fallback", lambda *args: None)
    scenario = CapabilityScenario(
        scenario_settings(tmp_path),
        model_providers=[ExhaustedProvider()],
        timing=ScenarioTiming(requirement_wait_seconds=5.0),
    )
    async with scenario:
        trace = await _run(scenario, _escalated, tag="exhausted-unadoptable")
    report = _report(trace)

    assert not trace.requirements, report
    escalations = trace.decisions("escalated")
    assert len(escalations) == 1, report
    assert split_legacy_payload(escalations[0].payload)[1]["evidence"][0] == "requirement", report
    assert trace.objective.status == "blocked", report
    assert all(task.started_at is None for task in trace.tasks_for(AgentId.FORGE)), report
    _assert_no_storm(trace, report)
