"""End-to-end capability workflow (spec section 22, Phase 5) through the real runtime.

The scenario submits one user objective ("add a ``text_statistics`` capability") to ORCHESTR
over the message bus and lets the real Engine, scheduler, security layer and all seven agents
work it: SCOUT and ROGUE research concurrently, SCOUT adopts an approach and creates the
requirement, WARDEN approves FORGE's process run, FORGE writes the module and its tests in its
task workspace, LEDGER runs the acceptance tests and decides the verdict, ORCHESTR completes the
objective and MUSE delivers the final update to the outbox.

Models are scripted (:class:`ScriptedCapabilityProvider`, no network) unless the caller passes
its own providers or provider settings (``aether demo capability --live``). Everything the
scenario reports is read back from the database (:func:`collect_trace`), never from memory.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

from sqlalchemy import select

from aether.agents.orchestr.limits import OrchestrLimits
from aether.agents.scout.schemas import ResearchLimits, SourceSpec
from aether.communication.contracts import ObjectiveCreatePayload, build_message
from aether.communication.messages import AgentId, MessageType
from aether.config import AetherSettings
from aether.models import MockProvider, ModelProvider, ProviderSettings
from aether.models.contracts import CompletionRequest, CompletionResponse
from aether.observability.recorder import split_legacy_payload
from aether.runtime.bootstrap import Runtime, build_runtime
from aether.runtime.engine import EngineLimits, ResourceSnapshotter
from aether.security.policy import SecuritySettings
from aether.storage.models import (
    Base,
    CapabilityRecord,
    EventRecord,
    ExecutionRunRecord,
    InteractionRecord,
    MessageRecord,
    ObjectiveRecord,
    RequirementRecord,
    ResearchFindingRecord,
    SecurityDecisionRecord,
    TaskDependencyRecord,
    TaskRecord,
    TestResultRecord,
    TestRunRecord,
)

OBJECTIVE_TITLE = "Add a text_statistics capability"
OBJECTIVE_DESCRIPTION = (
    "Provide a local Python function text_statistics(text) that, given a UTF-8 string, "
    "returns its word, line and character counts as JSON. No network and no package "
    "installation: the Python standard library is enough."
)
ACCEPTANCE_CRITERIA = (
    "AC1: text_statistics('a b c') reports 3 words",
    "AC2: text_statistics('') reports zero words, lines and characters",
    "AC3: non-ASCII text is counted by Unicode code points",
)
REFERENCE_PATH = "reference/text_statistics.md"
MODULE_FILE = "text_statistics.py"
TEST_FILE = "test_text_statistics.py"

REFERENCE_TEXT = """# Counting text statistics in Python

`str.split()` with no separator splits on runs of whitespace and returns `[]` for an empty
string, so `len(text.split())` is the word count. `str.splitlines()` returns the lines and is
empty for an empty string. `len(text)` counts Unicode code points, not encoded bytes.
`json.dumps` serialises the result.
"""

GOOD_MODULE = '''"""text_statistics: word, line and character counts for a UTF-8 string."""

import json
import sys


def text_statistics(text: str) -> dict[str, int]:
    return {
        "words": len(text.split()),
        "lines": len(text.splitlines()),
        "characters": len(text),
    }


def to_json(text: str) -> str:
    return json.dumps(text_statistics(text), sort_keys=True)


if __name__ == "__main__":
    print(to_json(" ".join(sys.argv[1:]) or "a b c"))
'''

# Splitting on a single space and counting encoded bytes: wrong for "" and non-ASCII text.
BUGGY_MODULE = '''"""text_statistics: word, line and character counts for a UTF-8 string."""

import json
import sys


def text_statistics(text: str) -> dict[str, int]:
    return {
        "words": len(text.split(" ")),
        "lines": text.count("\\n") + 1,
        "characters": len(text.encode("utf-8")),
    }


def to_json(text: str) -> str:
    return json.dumps(text_statistics(text), sort_keys=True)


if __name__ == "__main__":
    print(to_json(" ".join(sys.argv[1:]) or "a b c"))
'''

ACCEPTANCE_TESTS = '''"""Acceptance tests for text_statistics; test names carry the criterion ids (AC1..AC3)."""

import json

from text_statistics import text_statistics, to_json


def test_ac1_counts_three_words_for_a_b_c():
    assert text_statistics("a b c")["words"] == 3


def test_ac2_empty_string_reports_zeros():
    assert text_statistics("") == {"words": 0, "lines": 0, "characters": 0}


def test_ac3_non_ascii_counted_by_code_points():
    stats = text_statistics("na\\u00efve caf\\u00e9 \\u65e5\\u672c")
    assert stats["characters"] == 13
    assert stats["words"] == 3
    assert json.loads(to_json("\\u00e9"))["characters"] == 1
'''


# --------------------------------------------------------------------------------------------
# Scripted models
# --------------------------------------------------------------------------------------------


def _candidate(name: str, description: str, *, pros: list[str], cons: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "pros": pros,
        "cons": cons,
        "dependencies": [],
        "risks": [],
    }


STDLIB_APPROACH = _candidate(
    "stdlib-str-methods",
    "Count with str.split(), str.splitlines() and len() from the standard library",
    pros=["No dependencies", "Code-point counting is the default"],
    cons=["Whitespace-only word definition"],
)
REGEX_APPROACH = _candidate(
    "regex-tokenizer",
    "Tokenise words with a regular expression",
    pros=["Configurable word definition"],
    cons=["More code to test", "Easy to miscount non-ASCII words"],
)
STREAMING_APPROACH = _candidate(
    "streaming-counter",
    "Count characters incrementally from a byte stream",
    pros=["Constant memory for huge inputs"],
    cons=["Byte streams make code-point counting error-prone"],
)


def _plan_json(module: str, python: str) -> str:
    remove = (
        "import os, sys\nfor p in sys.argv[1:]:\n    if os.path.exists(p):\n        os.remove(p)\n"
    )
    return json.dumps(
        {
            "steps": [
                {
                    "tool": "write_file",
                    "arguments": {"path": MODULE_FILE, "content": module},
                    "purpose": "Write the text_statistics module",
                },
                {
                    "tool": "write_file",
                    "arguments": {"path": TEST_FILE, "content": ACCEPTANCE_TESTS},
                    "purpose": "Write the acceptance tests named after the criteria",
                },
                {
                    "tool": "run_process",
                    "arguments": {"argv": [python, "-m", "text_statistics", "a b c"]},
                    "purpose": "Smoke-run the module with the allowlisted interpreter",
                },
            ],
            "rollback_steps": [
                {
                    "tool": "run_process",
                    "arguments": {"argv": [python, "-c", remove, MODULE_FILE, TEST_FILE]},
                    "purpose": "Remove the files created by this plan",
                }
            ],
        }
    )


_EVIDENCE_INDEX = re.compile(r"^Evidence index (\d+):", re.MULTILINE)


class ScriptedCapabilityProvider(MockProvider):
    """Deterministic replies keyed by request shape; unknown requests get unusable text.

    ``latency_seconds`` keeps both researchers inside their model call at the same time, so
    concurrent research is observable from the persisted task timestamps.
    """

    def __init__(
        self,
        *,
        buggy: bool = False,
        latency_seconds: float = 0.3,
        python_executable: str = sys.executable,
    ) -> None:
        # "mock" is the provider name the budget policies know about.
        super().__init__(name="mock")
        self.buggy = buggy
        self.latency_seconds = latency_seconds
        self.python_executable = python_executable

    def reply(self, request: CompletionRequest) -> str:
        schema = request.json_schema or {}
        title = schema.get("title") if isinstance(schema, dict) else None
        system = request.messages[0].content if request.messages else ""
        user = request.messages[-1].content if request.messages else ""
        if title == "FindingDraft":
            evidence = sorted({int(match) for match in _EVIDENCE_INDEX.findall(user)})
            return json.dumps(
                {
                    "summary": "The standard library covers word, line and code-point counts.",
                    "candidates": [STDLIB_APPROACH, REGEX_APPROACH],
                    "recommendation": STDLIB_APPROACH["name"],
                    "confidence": 0.85 if evidence else 0.4,
                    "classification": "hypothesis",
                    "evidence_indices": evidence,
                    "security_considerations": ["Pure function; no I/O or network"],
                    "next_steps": ["Implement and verify offline"],
                }
            )
        if title == "DivergentDraft":
            return json.dumps({"candidates": [STREAMING_APPROACH, REGEX_APPROACH]})
        if title == "CritiqueDraft":
            return json.dumps(
                {
                    "ranked_indices": [0, 1],
                    "rationale": "Streaming is the most unconventional option but is unproven.",
                }
            )
        if system.startswith("You are FORGE"):
            module = BUGGY_MODULE if self.buggy else GOOD_MODULE
            return _plan_json(module, self.python_executable)
        if system.startswith("You are WARDEN"):
            return json.dumps(
                {
                    "summary": "Runs the allowlisted interpreter inside the task workspace.",
                    "risks": ["Executes generated code locally"],
                }
            )
        return "scripted provider: no reply for this request"

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        # MockProvider pops the next scripted reply synchronously, so this pairing is race-free.
        self._responses.appendleft(self.reply(request))
        return await super().complete(request)


# --------------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioTiming:
    """Fast polling so the scenario finishes in seconds; production keeps the defaults."""

    tick_seconds: float = 0.2
    poll_seconds: float = 0.05
    recovery_seconds: float = 1.0
    shutdown_seconds: float = 10.0


def scenario_settings(root: Path, *, database_url: str | None = None) -> AetherSettings:
    """Self-contained settings under ``root`` (SQLite unless ``database_url`` is given)."""
    return AetherSettings(
        _env_file=None,
        database_url=database_url or f"sqlite+aiosqlite:///{(root / 'aether.db').as_posix()}",
        data_dir=root / "data",
        log_dir=root / "logs",
        artifact_dir=root / "artifacts",
        workspace_dir=root / "workspaces",
        max_concurrent_tasks=4,
    )


def scenario_security(base: SecuritySettings | None = None) -> SecuritySettings:
    """The configured policy plus this interpreter on the executable allowlist.

    FORGE's smoke run and LEDGER's pytest run both use ``sys.executable``; every run still
    needs a WARDEN decision bound to its input hash.
    """
    base = base or SecuritySettings(_env_file=None)
    executables = list(base.allowed_executables)
    if sys.executable not in executables:
        executables.append(sys.executable)
    return base.model_copy(update={"allowed_executables": executables})


def scenario_runtime_settings(
    *, timing: ScenarioTiming, max_replans: int = 2, base: AetherSettings
) -> AetherSettings:
    """Return ``base`` with fast agent timing and scenario agent limits (runtime settings live on it)."""
    return base.model_copy(
        update={
            "agent_tick_seconds": timing.tick_seconds,
            "agent_tick_overrides": {},
            "task_control_poll_seconds": timing.tick_seconds,
            "task_interrupt_grace_seconds": 1.0,
            "orchestr": OrchestrLimits(llm_refinement=False, max_replans_per_objective=max_replans),
            "research": ResearchLimits(
                default_sources=(
                    SourceSpec(
                        kind="file",
                        location=REFERENCE_PATH,
                        purpose="Standard-library reference for text statistics",
                        perspective="conventional",
                    ),
                ),
            ),
        }
    )


def scenario_engine_limits(settings: AetherSettings, timing: ScenarioTiming) -> EngineLimits:
    return EngineLimits.from_settings(settings).model_copy(
        update={
            "scheduler_interval_seconds": timing.poll_seconds,
            "message_poll_seconds": timing.poll_seconds,
            "tick_poll_seconds": timing.poll_seconds,
            "recovery_interval_seconds": timing.recovery_seconds,
            "shutdown_timeout_seconds": timing.shutdown_seconds,
        }
    )


def write_reference_source(settings: AetherSettings) -> Path:
    """The local document SCOUT reads (inside the workspace root, so no network is needed)."""
    path = settings.workspace_dir / REFERENCE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(REFERENCE_TEXT, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CapabilityScenario:
    """One engine process working the scenario; build a new one on the same DB to restart."""

    settings: AetherSettings
    model_providers: Sequence[ModelProvider] | None = None
    provider_settings: ProviderSettings | None = None
    security_settings: SecuritySettings | None = None
    timing: ScenarioTiming = field(default_factory=ScenarioTiming)
    max_replans: int = 2
    create_schema: bool = True
    # None reads the real host; tests pin resources so machine load cannot defer or pause work.
    resource_monitor: ResourceSnapshotter | None = None
    runtime: Runtime | None = None

    async def start(self) -> Runtime:
        settings = scenario_runtime_settings(
            timing=self.timing, max_replans=self.max_replans, base=self.settings
        )
        write_reference_source(settings)
        runtime = build_runtime(
            settings,
            security_settings=scenario_security(self.security_settings),
            provider_settings=self.provider_settings
            or ProviderSettings(_env_file=None, mock=True, max_retries=0),
            model_providers=self.model_providers,
            engine_limits=scenario_engine_limits(settings, self.timing),
            resource_monitor=self.resource_monitor,
        )
        self.runtime = runtime
        try:
            if self.create_schema:
                async with runtime.database.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            # The engine directly: the caller owns logging configuration.
            await runtime.engine.start()
        except BaseException:
            await runtime.close()
            self.runtime = None
            raise
        return runtime

    async def stop(self) -> None:
        runtime, self.runtime = self.runtime, None
        if runtime is None:
            return
        try:
            await runtime.engine.stop()
        finally:
            await runtime.close()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def _runtime(self) -> Runtime:
        if self.runtime is None:
            raise RuntimeError("the scenario is not running")
        return self.runtime

    async def submit_objective(self, *, tag: str | None = None) -> UUID:
        """Submit the objective as the user (through MUSE) and wait until ORCHESTR stores it."""
        runtime = self._runtime()
        title = f"{OBJECTIVE_TITLE} ({tag or uuid4().hex[:8]})"
        # Only an objective created by this submission counts: earlier runs may share the title.
        submitted_at = datetime.now(UTC)
        message = build_message(
            AgentId.MUSE,
            AgentId.ORCHESTR,
            ObjectiveCreatePayload(
                title=title,
                description=OBJECTIVE_DESCRIPTION,
                priority=70,
                completion_criteria=list(ACCEPTANCE_CRITERIA),
                rationale="End-to-end capability workflow scenario",
            ),
        )
        await runtime.engine.bus.publish(message)

        async def created() -> UUID | None:
            async with runtime.database.sessions() as session:
                return await session.scalar(
                    select(ObjectiveRecord.id)
                    .where(
                        ObjectiveRecord.title == title,
                        ObjectiveRecord.created_at >= submitted_at,
                    )
                    .order_by(ObjectiveRecord.created_at.desc())
                    .limit(1)
                )

        return await wait_for(created, timeout=30.0, poll=self.timing.poll_seconds)

    async def trace(self, objective_id: UUID) -> ScenarioTrace:
        return await collect_trace(self._runtime(), objective_id)

    async def run_until(
        self,
        objective_id: UUID,
        predicate: Callable[[ScenarioTrace], bool],
        *,
        timeout: float,
        poll: float = 0.2,
    ) -> ScenarioTrace:
        """Poll persisted state until ``predicate`` holds; TimeoutError carries the last trace."""

        async def check() -> ScenarioTrace | None:
            trace = await self.trace(objective_id)
            return trace if predicate(trace) else None

        try:
            return await wait_for(check, timeout=timeout, poll=poll)
        except TimeoutError as error:
            last = await self.trace(objective_id)
            raise ScenarioTimeout(str(error), last) from None


class ScenarioTimeout(TimeoutError):
    def __init__(self, message: str, trace: ScenarioTrace) -> None:
        super().__init__(message)
        self.trace = trace


async def wait_for[T](
    probe: Callable[[], Awaitable[T | None]], *, timeout: float, poll: float = 0.1
) -> T:
    deadline = time.monotonic() + timeout
    while True:
        value = await probe()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"condition not reached within {timeout:g}s")
        await asyncio.sleep(poll)


# --------------------------------------------------------------------------------------------
# Persistent trace
# --------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScenarioTrace:
    """Everything the scenario did, read back from the database."""

    objective: ObjectiveRecord
    tasks: list[TaskRecord]
    dependencies: list[TaskDependencyRecord]
    transitions: list[EventRecord]
    findings: list[ResearchFindingRecord]
    requirements: list[RequirementRecord]
    capabilities: list[CapabilityRecord]
    security_decisions: list[SecurityDecisionRecord]
    scheduler_decisions: list[EventRecord]
    orchestr_decisions: list[EventRecord]
    requirement_events: list[EventRecord]
    execution_runs: list[ExecutionRunRecord]
    test_runs: list[TestRunRecord]
    test_results: list[TestResultRecord]
    interactions: list[InteractionRecord]
    messages: list[MessageRecord]
    outbox_paths: list[Path]

    @property
    def task_ids(self) -> set[UUID]:
        return {task.id for task in self.tasks}

    def tasks_for(self, agent: AgentId) -> list[TaskRecord]:
        return [task for task in self.tasks if task.assigned_agent == agent.value]

    def run_window(self, task_id: UUID) -> tuple[datetime, datetime | None] | None:
        """When the task first started running and when it finished, from ``tasks``.

        The second value is None while the task has not finished.
        """
        for task in self.tasks:
            if task.id == task_id and task.started_at is not None:
                ended = _aware(task.completed_at) if task.completed_at else None
                return _aware(task.started_at), ended
        return None

    def messages_of(
        self,
        message_type: MessageType,
        *,
        source: AgentId | None = None,
        target: AgentId | None = None,
    ) -> list[MessageRecord]:
        return [
            row
            for row in self.messages
            if row.message_type == message_type.value
            and (source is None or row.source_agent == source.value)
            and (target is None or row.target_agent == target.value)
        ]

    def decisions(self, name: str) -> list[EventRecord]:
        return [row for row in self.orchestr_decisions if _data(row).get("decision") == name]

    @property
    def verdicts(self) -> list[str]:
        """LEDGER's verdicts, in order, from the persisted ``test_runs.verdict``."""
        return [str(run.verdict) for run in self.test_runs if run.verdict]

    @property
    def completed(self) -> bool:
        return self.objective.status == "completed"

    @property
    def final_update_delivered(self) -> bool:
        """MUSE delivered an outbox file for this objective and told ORCHESTR about it."""
        return bool(self.outbox_paths) and bool(
            self.messages_of(
                MessageType.COMMUNICATION_DELIVERED, source=AgentId.MUSE, target=AgentId.ORCHESTR
            )
        )


def _data(row: EventRecord) -> dict[str, Any]:
    return split_legacy_payload(row.payload)[1]


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; every timestamp the runtime writes is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def collect_trace(runtime: Runtime, objective_id: UUID) -> ScenarioTrace:
    async with runtime.database.sessions() as session:
        objective = await session.get(ObjectiveRecord, objective_id)
        if objective is None:
            raise LookupError(f"objective {objective_id} does not exist")
        tasks = list(
            (
                await session.scalars(
                    select(TaskRecord)
                    .where(TaskRecord.objective_id == objective_id)
                    .order_by(TaskRecord.created_at, TaskRecord.id)
                )
            ).all()
        )
        task_ids = [task.id for task in tasks]
        dependencies = list(
            (
                await session.scalars(
                    select(TaskDependencyRecord).where(TaskDependencyRecord.task_id.in_(task_ids))
                )
            ).all()
        )
        transitions = list(
            (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.event_type == "task.transitioned",
                        EventRecord.aggregate_id.in_(task_ids),
                    )
                    .order_by(EventRecord.created_at, EventRecord.id)
                )
            ).all()
        )
        findings = list(
            (
                await session.scalars(
                    select(ResearchFindingRecord)
                    .where(ResearchFindingRecord.objective_id == objective_id)
                    .order_by(ResearchFindingRecord.created_at)
                )
            ).all()
        )
        requirements = list(
            (
                await session.scalars(
                    select(RequirementRecord)
                    .where(RequirementRecord.objective_id == objective_id)
                    .order_by(RequirementRecord.created_at)
                )
            ).all()
        )
        capabilities = list(
            (
                await session.scalars(
                    select(CapabilityRecord)
                    .where(CapabilityRecord.objective_id == objective_id)
                    .order_by(CapabilityRecord.created_at, CapabilityRecord.name)
                )
            ).all()
        )
        security_decisions = list(
            (
                await session.scalars(
                    select(SecurityDecisionRecord)
                    .where(SecurityDecisionRecord.task_id.in_(task_ids))
                    .order_by(SecurityDecisionRecord.created_at)
                )
            ).all()
        )
        scheduler_decisions = list(
            (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.event_type == "scheduler.decision",
                        EventRecord.task_id.in_(task_ids),
                    )
                    .order_by(EventRecord.created_at)
                )
            ).all()
        )
        orchestr_decisions = list(
            (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.event_type == "decision.recorded",
                        EventRecord.objective_id == objective_id,
                    )
                    .order_by(EventRecord.created_at)
                )
            ).all()
        )
        requirement_events = list(
            (
                await session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.aggregate_type == "requirement",
                        EventRecord.objective_id == objective_id,
                    )
                    .order_by(EventRecord.created_at)
                )
            ).all()
        )
        execution_runs = list(
            (
                await session.scalars(
                    select(ExecutionRunRecord)
                    .where(ExecutionRunRecord.task_id.in_(task_ids))
                    .order_by(ExecutionRunRecord.started_at)
                )
            ).all()
        )
        capability_ids = [capability.id for capability in capabilities]
        test_runs = list(
            (
                await session.scalars(
                    select(TestRunRecord)
                    .where(
                        TestRunRecord.capability_id.in_(capability_ids)
                        | TestRunRecord.task_id.in_(task_ids)
                    )
                    .order_by(TestRunRecord.started_at)
                )
            ).all()
        )
        test_results = list(
            (
                await session.scalars(
                    select(TestResultRecord)
                    .where(TestResultRecord.test_run_id.in_([run.id for run in test_runs]))
                    .order_by(TestResultRecord.name)
                )
            ).all()
        )
        interactions = list(
            (
                await session.scalars(
                    select(InteractionRecord)
                    .where(InteractionRecord.objective_id == objective_id)
                    .order_by(InteractionRecord.occurred_at)
                )
            ).all()
        )
        messages = list(
            (
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        (MessageRecord.objective_id == objective_id)
                        | MessageRecord.task_id.in_(task_ids)
                    )
                    .order_by(MessageRecord.created_at)
                )
            ).all()
        )
        outbox_events = (
            await session.scalars(
                select(EventRecord)
                .where(
                    EventRecord.event_type == "decision.recorded",
                    EventRecord.objective_id == objective_id,
                    EventRecord.source.startswith("agents.muse"),
                )
                .order_by(EventRecord.created_at)
            )
        ).all()
    outbox_paths: list[Path] = []
    for event in outbox_events:
        data = _data(event)
        if data.get("decision") != "muse.outbox" or not data.get("evidence"):
            continue
        try:
            path = json.loads(data["evidence"][0]).get("path")
        except (TypeError, ValueError):
            continue
        if path and Path(path) not in outbox_paths:
            outbox_paths.append(Path(path))
    return ScenarioTrace(
        objective=objective,
        tasks=tasks,
        dependencies=dependencies,
        transitions=transitions,
        findings=findings,
        requirements=requirements,
        capabilities=capabilities,
        security_decisions=security_decisions,
        scheduler_decisions=scheduler_decisions,
        orchestr_decisions=[
            row for row in orchestr_decisions if not row.source.startswith("agents.muse")
        ],
        requirement_events=requirement_events,
        execution_runs=execution_runs,
        test_runs=test_runs,
        test_results=test_results,
        interactions=interactions,
        messages=messages,
        outbox_paths=outbox_paths,
    )


def _when(value: datetime | None) -> str:
    if value is None:
        return "-"
    value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%H:%M:%S.%f")[:-3]


def _short(value: UUID | None) -> str:
    return str(value)[:8] if value is not None else "-"


def format_trace(trace: ScenarioTrace) -> list[str]:
    """A step-by-step, human-readable report of the persisted workflow."""
    lines: list[str] = []
    objective = trace.objective
    lines.append(f"Objective {_short(objective.id)} '{objective.title}' status={objective.status}")
    for criterion in objective.completion_criteria:
        lines.append(f"  criterion: {criterion}")
    names = {task.id: task.title.splitlines()[0] for task in trace.tasks}
    lines.append(f"Tasks ({len(trace.tasks)}):")
    for task in trace.tasks:
        deps = [
            _short(dep.depends_on_task_id) for dep in trace.dependencies if dep.task_id == task.id
        ]
        window = trace.run_window(task.id)
        started, ended = window if window is not None else (None, None)
        lines.append(
            f"  {_short(task.id)} {task.assigned_agent or '-':8} {task.state:22} "
            f"{_when(started)} -> {_when(ended)} {task.title.splitlines()[0]}"
            + (f" (after {', '.join(deps)})" if deps else "")
        )
    lines.append(f"Research findings ({len(trace.findings)}):")
    for finding in trace.findings:
        lines.append(
            f"  {finding.agent:6} {finding.status:15} confidence={finding.confidence:.2f} "
            f"recommends={finding.recommendation or '-'}; {finding.rationale or '-'}"
        )
    lines.append(f"Requirements ({len(trace.requirements)}):")
    for requirement in trace.requirements:
        lines.append(
            f"  {_short(requirement.id)} {requirement.status:11} by {requirement.created_by}: "
            f"{requirement.title}"
        )
        for criterion in requirement.acceptance_criteria:
            lines.append(f"    - {criterion}")
    lines.append(f"WARDEN decisions ({len(trace.security_decisions)}):")
    for decision in trace.security_decisions:
        lines.append(
            f"  {decision.decision:9} {decision.operation} task={_short(decision.task_id)} "
            f"input_hash={decision.input_hash[:16]}"
        )
    lines.append(f"Scheduler decisions: {len(trace.scheduler_decisions)}")
    for event in trace.scheduler_decisions[:20]:
        task_id = event.task_id
        lines.append(f"  {_when(event.created_at)} selected {names.get(task_id, _short(task_id))}")
    lines.append(f"ORCHESTR decisions ({len(trace.orchestr_decisions)}):")
    for event in trace.orchestr_decisions:
        data = _data(event)
        lines.append(f"  {data.get('decision')}: {data.get('rationale')}")
    lines.append(f"Execution runs ({len(trace.execution_runs)}):")
    for run in trace.execution_runs:
        lines.append(f"  {run.agent:6} {run.operation[:70]:70} {run.status:9} exit={run.exit_code}")
    lines.append(f"Test runs ({len(trace.test_runs)}):")
    for test_run in trace.test_runs:
        lines.append(
            f"  {test_run.suite} status={test_run.status} "
            f"passed={test_run.passed} failed={test_run.failed} errors={test_run.errors}"
        )
        for result in test_run.acceptance_results:
            mark = "PASS" if result.get("passed") else "FAIL"
            lines.append(f"    [{mark}] {result.get('criterion')}")
    lines.append(f"Test results ({len(trace.test_results)}):")
    for result in trace.test_results:
        lines.append(f"  {result.outcome:7} {result.name}")
    lines.append(f"LEDGER verdicts: {', '.join(trace.verdicts) or '-'}")
    lines.append(f"Capabilities ({len(trace.capabilities)}):")
    for capability in trace.capabilities:
        lines.append(
            f"  {capability.lifecycle_status:10} verification={capability.verification_status} "
            f"{capability.name}"
        )
    blockers = trace.messages_of(MessageType.BLOCKER)
    if blockers:
        lines.append(f"Blockers ({len(blockers)}):")
        for row in blockers:
            lines.append(
                f"  {row.source_agent} -> {row.target_agent}: {row.payload.get('description')}"
            )
    lines.append(f"Interactions: {len(trace.interactions)}")
    delivered = trace.messages_of(MessageType.COMMUNICATION_DELIVERED)
    lines.append(f"communication.delivered messages: {len(delivered)}")
    for path in trace.outbox_paths:
        lines.append(f"Outbox: {path}")
    return lines


def capability_finished(trace: ScenarioTrace) -> bool:
    """Happy path: the objective completed and the final update reached the outbox."""
    return trace.completed and trace.final_update_delivered


def verification_failed(trace: ScenarioTrace) -> bool:
    """Failing path: LEDGER failed the run and routed the blocker back to FORGE."""
    return "failed" in trace.verdicts and bool(
        trace.messages_of(MessageType.BLOCKER, source=AgentId.LEDGER, target=AgentId.FORGE)
    )


def forge_operation_approved(trace: ScenarioTrace) -> bool:
    """WARDEN approved a FORGE operation (step 6)."""
    forge_tasks = {task.id for task in trace.tasks_for(AgentId.FORGE)}
    return any(
        decision.task_id in forge_tasks and decision.decision == "approved"
        for decision in trace.security_decisions
    )


__all__ = [
    "ACCEPTANCE_CRITERIA",
    "BUGGY_MODULE",
    "GOOD_MODULE",
    "MODULE_FILE",
    "OBJECTIVE_TITLE",
    "TEST_FILE",
    "CapabilityScenario",
    "ScenarioTimeout",
    "ScenarioTiming",
    "ScenarioTrace",
    "ScriptedCapabilityProvider",
    "capability_finished",
    "collect_trace",
    "forge_operation_approved",
    "format_trace",
    "scenario_engine_limits",
    "scenario_runtime_settings",
    "scenario_security",
    "scenario_settings",
    "verification_failed",
    "wait_for",
    "write_reference_source",
]
