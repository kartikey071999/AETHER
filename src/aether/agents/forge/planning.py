"""Plan building: workspace scoping, deterministic templates and the validated LLM draft."""

import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError

from aether.agents.base import AgentContext
from aether.agents.forge.prompts import DRAFT_SYSTEM_PROMPT, draft_user_prompt
from aether.agents.forge.schemas import (
    ARTIFACTS_DIR,
    MANIFEST_PATH,
    RUNTIME_COPY_DIR,
    DraftPlan,
    ForgeConfig,
    ForgePlan,
    ForgeSpec,
    PlanDefect,
    RuntimeScopeDefect,
)
from aether.communication.contracts import (
    PlanStep,
    ProgressUpdatePayload,
    ResourceEstimate,
    build_message,
)
from aether.communication.messages import AgentId
from aether.models.contracts import CompletionMessage, CompletionRequest, ModelTier
from aether.models.facade import ModelCallContext
from aether.storage.models import TaskRecord

logger = logging.getLogger("aether.agent.forge")

FORGE_NAMESPACE = uuid5(NAMESPACE_URL, "https://aether.local/agents/forge")
_SPEC_BLOCK = re.compile(r"```forge-spec\s*\n(.*?)\n\s*```", re.DOTALL)
_JSON_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)
_PINNED_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9._!+-]*$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_REMOVE_PATHS = (
    "import os, shutil, sys\n"
    "for p in sys.argv[1:]:\n"
    "    if os.path.isdir(p):\n"
    "        shutil.rmtree(p, ignore_errors=True)\n"
    "    elif os.path.exists(p):\n"
    "        os.remove(p)\n"
)
_FILE_TOOLS = frozenset({"write_file", "read_file"})


def forge_id(*parts: object) -> UUID:
    """Deterministic id so redelivered work publishes the same messages."""
    return uuid5(FORGE_NAMESPACE, ":".join(str(part) for part in parts))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def relative_path(raw: object, task_id: UUID, *, allow_empty: bool = False) -> str:
    """A confined, task-relative POSIX path; absolute paths and `..` are plan defects."""
    if not isinstance(raw, str) or "\x00" in raw:
        raise PlanDefect("path must be a string")
    text = raw.replace("\\", "/").strip()
    if text.startswith("/") or _DRIVE.match(text):
        raise PlanDefect("absolute paths are not allowed; paths are task-workspace relative")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PlanDefect("'..' segments are not allowed in paths")
    if parts and parts[0] == str(task_id):
        parts = parts[1:]
    if not parts and not allow_empty:
        raise PlanDefect("path must not be empty")
    return "/".join(parts)


def _within_runtime_copy(relative: str) -> bool:
    head = relative.split("/", 1)[0]
    return head in (RUNTIME_COPY_DIR, ARTIFACTS_DIR) or relative == ".forge/manifest.json"


def touches_runtime_copy(step: PlanStep, task_id: UUID) -> bool:
    for key in ("path", "cwd"):
        value = step.arguments.get(key)
        if isinstance(value, str):
            try:
                if relative_path(value, task_id, allow_empty=True).split("/", 1)[0] == (
                    RUNTIME_COPY_DIR
                ):
                    return True
            except PlanDefect:
                continue
    return False


def scope_step(
    step: PlanStep, task_id: UUID, *, runtime: bool, config: ForgeConfig | None = None
) -> PlanStep:
    """Rewrite a step so every path and cwd lives under ``<task_id>/``; idempotent."""
    arguments: dict[str, Any] = dict(step.arguments)
    prefix = str(task_id)
    if step.tool in _FILE_TOOLS:
        rel = relative_path(arguments.get("path"), task_id)
        if runtime and not _within_runtime_copy(rel):
            raise RuntimeScopeDefect(
                f"runtime changes are confined to {RUNTIME_COPY_DIR}/ and {ARTIFACTS_DIR}/"
            )
        arguments["path"] = f"{prefix}/{rel}"
    elif step.tool == "run_process":
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise PlanDefect("run_process requires a non-empty argv list of strings")
        raw_cwd = arguments.get("cwd")
        default = RUNTIME_COPY_DIR if runtime else ""
        rel = (
            default if raw_cwd in (None, "") else relative_path(raw_cwd, task_id, allow_empty=True)
        )
        if runtime and not _within_runtime_copy(rel or "."):
            raise RuntimeScopeDefect(f"runtime processes must run inside {RUNTIME_COPY_DIR}/")
        arguments["cwd"] = f"{prefix}/{rel}" if rel else prefix
        if (
            config is not None
            and config.step_timeout_seconds
            and "timeout_seconds" not in arguments
        ):
            arguments["timeout_seconds"] = config.step_timeout_seconds
    return PlanStep(tool=step.tool, arguments=arguments, purpose=step.purpose)


def scope_plan(plan: ForgePlan, config: ForgeConfig) -> ForgePlan:
    runtime = plan.runtime_change or any(
        touches_runtime_copy(step, plan.task_id) for step in (*plan.steps, *plan.rollback_steps)
    )

    def scoped(steps: Iterable[PlanStep]) -> tuple[PlanStep, ...]:
        return tuple(scope_step(s, plan.task_id, runtime=runtime, config=config) for s in steps)

    return replace(
        plan,
        steps=scoped(plan.steps),
        rollback_steps=scoped(plan.rollback_steps),
        runtime_change=runtime,
    )


def manifest_step(plan: ForgePlan) -> PlanStep:
    content = json.dumps(
        {
            "plan_id": str(plan.plan_id),
            "task_id": str(plan.task_id),
            "source": plan.source,
            "runtime_change": plan.runtime_change,
            "steps": [{"tool": s.tool, "purpose": s.purpose} for s in plan.steps],
            "rollback_steps": [{"tool": s.tool, "purpose": s.purpose} for s in plan.rollback_steps],
        },
        sort_keys=True,
        indent=2,
    )
    return PlanStep(
        tool="write_file",
        arguments={"path": f"{plan.task_id}/{MANIFEST_PATH}", "content": content, "append": False},
        purpose="Create the isolated task workspace and record the plan manifest",
    )


def extract_spec(*texts: str) -> ForgeSpec | None:
    """The first ``forge-spec`` block found; malformed specs are plan defects, not ignored."""
    for text in texts:
        match = _SPEC_BLOCK.search(text or "")
        if match is None:
            continue
        try:
            return ForgeSpec.model_validate_json(match.group(1))
        except ValidationError as error:
            raise PlanDefect(
                f"forge-spec block is invalid: {error.error_count()} error(s)"
            ) from None
    return None


def _write(path: str, content: str, purpose: str) -> PlanStep:
    return PlanStep(
        tool="write_file",
        arguments={"path": path, "content": content, "append": False},
        purpose=purpose,
    )


def _run(python: str, args: list[str], purpose: str, cwd: str | None = None) -> PlanStep:
    arguments: dict[str, Any] = {"argv": [python, *args]}
    if cwd:
        arguments["cwd"] = cwd
    return PlanStep(tool="run_process", arguments=arguments, purpose=purpose)


def _remove(python: str, paths: list[str], purpose: str, cwd: str | None = None) -> PlanStep:
    return _run(python, ["-c", _REMOVE_PATHS, *paths], purpose, cwd)


def _template_steps(
    spec: ForgeSpec, task_id: UUID, config: ForgeConfig
) -> tuple[list[PlanStep], list[PlanStep], ResourceEstimate, bool]:
    python = config.python_executable
    if spec.kind == "pip_install":
        if not spec.packages:
            raise PlanDefect("pip_install requires at least one pinned package")
        for package in spec.packages:
            if not _PINNED_PACKAGE.match(package):
                raise PlanDefect("packages must be exact pins such as name==1.2.3")
        venv_python = "venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python"
        steps = [
            _run(python, ["-m", "venv", "venv"], "Create an isolated virtual environment"),
            _run(
                python,
                [
                    "-m",
                    "pip",
                    "--python",
                    venv_python,
                    "install",
                    "--no-input",
                    "--disable-pip-version-check",
                    *spec.packages,
                ],
                "Install pinned packages into the isolated environment",
            ),
        ]
        rollback = [_remove(python, ["venv"], "Remove the isolated environment")]
        return steps, rollback, ResourceEstimate(ram_mb=512, disk_mb=512, duration_s=600), False

    if not spec.files:
        raise PlanDefect(f"{spec.kind} requires at least one file")
    runtime = spec.kind == "runtime_change"
    base = f"{RUNTIME_COPY_DIR}/" if runtime else ""
    paths = [f"{base}{relative_path(name, task_id)}" for name in spec.files]
    steps = [
        _write(f"{base}{relative_path(name, task_id)}", content, f"Write {name}")
        for name, content in spec.files.items()
    ]
    cwd = RUNTIME_COPY_DIR if runtime else None
    local = [path.removeprefix(base) for path in paths]
    rollback = [_remove(python, local, "Remove the files created by this plan", cwd)]
    if spec.kind == "python_test":
        command = list(spec.command)
        if not command:
            modules = [name for name in spec.files if name.endswith(".py")]
            if len(modules) != 1:
                raise PlanDefect("python_test needs a command when it writes several modules")
            command = ["-m", relative_path(modules[0], task_id)[: -len(".py")].replace("/", ".")]
        steps.append(_run(python, command, "Run the module under test"))
    return (
        steps,
        rollback,
        ResourceEstimate(ram_mb=256, disk_mb=64, cpu_percent=25, duration_s=120),
        runtime,
    )


def build_template_plan(
    spec: ForgeSpec,
    task: TaskRecord,
    *,
    requirement_id: UUID | None,
    criteria: Iterable[str],
    config: ForgeConfig,
) -> ForgePlan:
    steps, rollback, estimate, runtime = _template_steps(spec, task.id, config)
    return ForgePlan(
        plan_id=forge_id("plan", task.id, "template", _digest(spec.model_dump_json())),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=requirement_id,
        steps=tuple(steps),
        rollback_steps=tuple(rollback),
        acceptance_criteria=tuple(criteria),
        resource_estimate=estimate,
        source=f"template:{spec.kind}",
        runtime_change=runtime,
    )


def _json_text(raw: str) -> str:
    text = raw.strip()
    fenced = _JSON_FENCE.match(text)
    return fenced.group(1) if fenced else text


DRAFT_ATTEMPT_KEY = "forge_draft_attempt"


def draft_attempt_id(task_id: UUID) -> UUID:
    return forge_id("draft-attempt", task_id)


async def _claim_draft_attempt(ctx: AgentContext, task: TaskRecord) -> bool:
    """Record the task's single LLM draft attempt; ``False`` when it was already made."""
    message = build_message(
        AgentId.FORGE,
        AgentId.ORCHESTR,
        ProgressUpdatePayload(
            task_id=task.id,
            objective_id=task.objective_id,
            summary="FORGE is drafting an implementation plan with an LLM (one attempt per task)",
            details={DRAFT_ATTEMPT_KEY: True},
        ),
        message_id=draft_attempt_id(task.id),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=task.requirement_id,
    )
    return await ctx.bus.publish(message)


async def draft_with_llm(
    ctx: AgentContext,
    task: TaskRecord,
    *,
    requirement_id: UUID | None,
    criteria: tuple[str, ...],
    registered_tools: Mapping[str, tuple[str, object]],
    config: ForgeConfig,
) -> ForgePlan | None:
    """A `standard`-tier draft, accepted only as validated JSON; any failure yields ``None``.

    FORGE asks at most once per task: a durable marker message records the attempt, so a
    redelivered assignment, a re-run after approval or a resolution attempt never re-drafts.
    """
    if ctx.models is None or not config.llm_drafting or not registered_tools:
        return None
    if not await _claim_draft_attempt(ctx, task):
        logger.info("forge LLM draft skipped: this task already had its one draft attempt")
        return None
    call = ModelCallContext(task_id=task.id, objective_id=task.objective_id)
    prompt = draft_user_prompt(
        title=task.title,
        description=task.description,
        criteria=criteria,
        tools=((name, desc, schema) for name, (desc, schema) in sorted(registered_tools.items())),
        max_steps=config.max_steps,
    )
    request = CompletionRequest(
        messages=(
            CompletionMessage(role="system", content=DRAFT_SYSTEM_PROMPT),
            CompletionMessage(role="user", content=prompt),
        ),
        max_tokens=config.llm_max_tokens,
        response_format="json_object",
        tier=ModelTier.STANDARD,
    )
    try:
        response = await ctx.models.complete(request, call)
    except Exception as error:  # noqa: BLE001 - unavailable, over budget or blocked: no draft
        logger.warning("forge LLM draft unavailable: %s", type(error).__name__)
        return None
    try:
        text = _json_text(response.text)
        draft = DraftPlan.model_validate_json(text)
        steps = tuple(
            PlanStep(tool=s.tool, arguments=json.loads(json.dumps(s.arguments)), purpose=s.purpose)
            for s in draft.steps
        )
        rollback = tuple(
            PlanStep(tool=s.tool, arguments=json.loads(json.dumps(s.arguments)), purpose=s.purpose)
            for s in draft.rollback_steps
        )
    except Exception as error:  # noqa: BLE001 - any parse failure means "no draft"
        logger.warning("forge LLM draft discarded: %s", type(error).__name__)
        await ctx.models.report_unusable(call, reason="invalid_plan_json")
        return None
    if any(step.tool not in registered_tools for step in (*steps, *rollback)):
        logger.warning("forge LLM draft discarded: it used an unregistered tool")
        await ctx.models.report_unusable(call, reason="unregistered_tool")
        return None
    return ForgePlan(
        plan_id=forge_id("plan", task.id, "llm", _digest(text)),
        task_id=task.id,
        objective_id=task.objective_id,
        requirement_id=requirement_id,
        steps=steps,
        rollback_steps=rollback,
        acceptance_criteria=criteria,
        resource_estimate=ResourceEstimate(ram_mb=512, disk_mb=256, cpu_percent=50, duration_s=300),
        source="llm",
    )
