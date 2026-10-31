"""Workflow engine: code, not an agent's model, executes workflows step by step.

Runs and step state are persisted; ``resume`` continues an interrupted run after its last finished
step, reusing stored outputs, so completed side effects are never repeated. Every step and run
outcome is recorded as a typed event in the same transaction as the state change.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.observability.events import build_event
from aether.observability.recorder import EventRecorder
from aether.skills.base import SkillContext, SkillError, execute_skill
from aether.skills.registry import SkillRegistry
from aether.storage.models import (
    RunStatus,
    WorkflowDefinitionRecord,
    WorkflowRunRecord,
    WorkflowStatus,
    WorkflowStepRunRecord,
)
from aether.storage.repositories._events import to_json
from aether.workflows.definition import (
    WorkflowDefinition,
    WorkflowDefinitionError,
    lookup,
    resolve,
)

# Only verified workflows run for real (approved ones are dry-run by LEDGER first).
RUNNABLE = frozenset({WorkflowStatus.VERIFIED})


class WorkflowNotFound(LookupError):
    pass


@dataclass(frozen=True)
class WorkflowResult:
    run_id: UUID
    status: str
    outputs: dict[str, Any] | None
    error: str | None


class WorkflowEngine:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        skills: SkillRegistry,
        recorder: EventRecorder | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.skills = skills
        self.recorder = recorder or EventRecorder()
        self.clock = clock

    # -- definitions -----------------------------------------------------------------------

    async def register(
        self,
        definition: WorkflowDefinition,
        *,
        status: WorkflowStatus,
        built_in: bool,
        created_by: str,
    ) -> UUID:
        """Store a validated definition; re-registering the same content is a no-op."""
        definition.validate_skills(self.skills)
        digest = definition.definition_hash
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(WorkflowDefinitionRecord).where(
                    WorkflowDefinitionRecord.name == definition.name,
                    WorkflowDefinitionRecord.version == definition.version,
                )
            )
            if existing is not None:
                if existing.definition_hash != digest:
                    raise WorkflowDefinitionError(
                        f"{definition.name} v{definition.version} already exists with other content"
                    )
                return existing.id
            record = WorkflowDefinitionRecord(
                name=definition.name,
                version=definition.version,
                owner_agent=definition.owner.value,
                status=status,
                built_in=built_in,
                definition=definition.model_dump(mode="json"),
                definition_hash=digest,
                created_by=created_by,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            session.add(record)
            await session.flush()
            self._event(
                session,
                "workflow.definition_registered",
                created_by,
                record.id,
                {
                    "name": definition.name,
                    "version": definition.version,
                    "owner_agent": definition.owner.value,
                    "status": str(status),
                    "built_in": built_in,
                    "steps": len(definition.steps),
                    "definition_hash": digest,
                },
            )
            return record.id

    async def load(self, name: str, version: int | None = None) -> tuple[WorkflowDefinition, str]:
        """The named definition (latest version unless given) and its lifecycle status."""
        async with self.sessions() as session:
            statement = select(WorkflowDefinitionRecord).where(
                WorkflowDefinitionRecord.name == name
            )
            if version is not None:
                statement = statement.where(WorkflowDefinitionRecord.version == version)
            record = await session.scalar(
                statement.order_by(WorkflowDefinitionRecord.version.desc()).limit(1)
            )
        if record is None:
            raise WorkflowNotFound(f"unknown workflow: {name}")
        return WorkflowDefinition.model_validate(record.definition), record.status

    # -- runs ------------------------------------------------------------------------------

    async def run(
        self,
        definition: WorkflowDefinition,
        inputs: dict[str, Any],
        ctx: SkillContext,
        *,
        idempotency_key: str | None = None,
    ) -> WorkflowResult:
        run_id = await self.start(definition, inputs, ctx, idempotency_key=idempotency_key)
        return await self.execute(run_id, definition, ctx)

    async def start(
        self,
        definition: WorkflowDefinition,
        inputs: dict[str, Any],
        ctx: SkillContext,
        *,
        idempotency_key: str | None = None,
    ) -> UUID:
        if ctx.agent_id is not definition.owner:
            raise WorkflowDefinitionError(
                f"{ctx.agent_id.value} may not run {definition.name} (owner {definition.owner.value})"
            )
        definition.validate_skills(self.skills)
        definition.check_inputs(inputs)
        async with self.sessions() as session, session.begin():
            if idempotency_key is not None:
                existing = await session.scalar(
                    select(WorkflowRunRecord.id).where(
                        WorkflowRunRecord.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return existing
            run = WorkflowRunRecord(
                workflow_name=definition.name,
                workflow_version=definition.version,
                agent=ctx.agent_id.value,
                objective_id=ctx.objective_id,
                task_id=ctx.task_id,
                status=RunStatus.PENDING,
                inputs=to_json(inputs),
                dry_run=ctx.dry_run,
                idempotency_key=idempotency_key,
                started_at=self.clock(),
            )
            session.add(run)
            await session.flush()
            for step in definition.steps:
                session.add(WorkflowStepRunRecord(run_id=run.id, step_id=step.id, skill=step.skill))
            return run.id

    async def execute(
        self, run_id: UUID, definition: WorkflowDefinition, ctx: SkillContext
    ) -> WorkflowResult:
        async with self.sessions() as session, session.begin():
            run = await session.get(WorkflowRunRecord, run_id)
            if run is None:
                raise WorkflowNotFound(f"unknown workflow run: {run_id}")
            if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
                return WorkflowResult(run.id, run.status, run.outputs, run.error)
            resumed = run.status == RunStatus.RUNNING
            run.status = RunStatus.RUNNING
            inputs = dict(run.inputs)
            self._event(
                session,
                "workflow.run_started",
                ctx.agent_id.value,
                run.id,
                {
                    "workflow": definition.name,
                    "version": definition.version,
                    "agent": ctx.agent_id.value,
                    "steps": len(definition.steps),
                    "dry_run": ctx.dry_run,
                    "resumed": resumed,
                },
                ctx,
            )
        scope: dict[str, Any] = {"input": inputs, "steps": {}}
        succeeded = skipped = 0
        failure: str | None = None
        for step in definition.steps:
            state = await self._step_state(run_id, step.id)
            if state.status == RunStatus.SUCCEEDED:
                scope["steps"][step.id] = {"output": state.output}
                succeeded += 1
                continue
            if state.status == RunStatus.CANCELLED:  # skipped earlier
                scope["steps"][step.id] = {"output": None}
                skipped += 1
                continue
            if step.when is not None and not resolve(step.when, scope):
                await self._finish_step(
                    run_id, definition, step, ctx, "skipped", state.attempts, None, None, 0
                )
                scope["steps"][step.id] = {"output": None}
                skipped += 1
                continue
            output, error, attempts, duration = await self._attempt(
                step, scope, ctx, state.attempts
            )
            if error is None:
                await self._finish_step(
                    run_id, definition, step, ctx, "succeeded", attempts, output, None, duration
                )
                scope["steps"][step.id] = {"output": output}
                succeeded += 1
                continue
            await self._finish_step(
                run_id, definition, step, ctx, "failed", attempts, None, error, duration
            )
            scope["steps"][step.id] = {"output": None}
            if step.on_error == "fail":
                failure = f"step {step.id} failed: {error}"
                break
        outputs = (
            None
            if failure
            else {name: resolve(reference, scope) for name, reference in definition.outputs.items()}
        )
        return await self._finish_run(run_id, definition, ctx, failure, outputs, succeeded, skipped)

    async def resume(self, run_id: UUID, ctx: SkillContext) -> WorkflowResult:
        async with self.sessions() as session:
            run = await session.get(WorkflowRunRecord, run_id)
        if run is None:
            raise WorkflowNotFound(f"unknown workflow run: {run_id}")
        definition, _ = await self.load(run.workflow_name, run.workflow_version)
        return await self.execute(run_id, definition, ctx)

    # -- internals -------------------------------------------------------------------------

    async def _attempt(self, step, scope, ctx: SkillContext, prior_attempts: int):
        attempts = prior_attempts
        error: str | None = None
        duration = 0.0
        for _ in range(step.retries + 1):
            attempts += 1
            try:
                result, duration = await execute_skill(
                    self.skills.get(step.skill), resolve(step.inputs, scope), ctx
                )
                return to_json(result.model_dump(mode="json")), None, attempts, duration
            except SkillError as caught:
                error = str(caught)[:500]
            except Exception as caught:  # noqa: BLE001 - a crashing skill fails its step
                error = f"{step.skill}: {type(caught).__name__}"
        return None, error, attempts, duration

    async def _step_state(self, run_id: UUID, step_id: str) -> WorkflowStepRunRecord:
        async with self.sessions() as session:
            state = await session.scalar(
                select(WorkflowStepRunRecord).where(
                    WorkflowStepRunRecord.run_id == run_id, WorkflowStepRunRecord.step_id == step_id
                )
            )
        assert state is not None
        return state

    async def _finish_step(
        self, run_id, definition, step, ctx, status, attempts, output, error, duration
    ):
        stored = {
            "succeeded": RunStatus.SUCCEEDED,
            "failed": RunStatus.FAILED,
            "skipped": RunStatus.CANCELLED,
        }
        async with self.sessions() as session, session.begin():
            state = await session.scalar(
                select(WorkflowStepRunRecord)
                .where(
                    WorkflowStepRunRecord.run_id == run_id, WorkflowStepRunRecord.step_id == step.id
                )
                .with_for_update()
            )
            state.status = stored[status]
            state.attempts = attempts
            state.output = output
            state.error = error
            state.started_at = state.started_at or self.clock()
            state.finished_at = self.clock()
            self._event(
                session,
                "workflow.step_finished",
                ctx.agent_id.value,
                run_id,
                {
                    "workflow": definition.name,
                    "step": step.id,
                    "skill": step.skill,
                    "status": status,
                    "attempts": attempts,
                    "duration_ms": round(duration, 3),
                    "error": error,
                },
                ctx,
            )

    async def _finish_run(self, run_id, definition, ctx, failure, outputs, succeeded, skipped):
        status = RunStatus.FAILED if failure else RunStatus.SUCCEEDED
        async with self.sessions() as session, session.begin():
            run = await session.get(WorkflowRunRecord, run_id, with_for_update=True)
            run.status = status
            run.outputs = to_json(outputs) if outputs is not None else None
            run.error = failure
            run.finished_at = self.clock()
            self._event(
                session,
                "workflow.run_finished",
                ctx.agent_id.value,
                run_id,
                {
                    "workflow": definition.name,
                    "version": definition.version,
                    "status": str(status),
                    "steps_succeeded": succeeded,
                    "steps_skipped": skipped,
                    "error": failure,
                },
                ctx,
            )
        return WorkflowResult(run_id, status, outputs, failure)

    def _event(
        self, session, event_type, actor, aggregate_id, payload, ctx: SkillContext | None = None
    ):
        self.recorder.add(
            session,
            build_event(
                event_type,
                source="aether.workflows",
                actor=actor,
                aggregate_type="workflow",
                aggregate_id=aggregate_id,
                correlation_id=(ctx.correlation_id if ctx else None) or aggregate_id,
                objective_id=ctx.objective_id if ctx else None,
                task_id=ctx.task_id if ctx else None,
                payload=payload,
            ),
        )


__all__ = ["RUNNABLE", "WorkflowEngine", "WorkflowNotFound", "WorkflowResult", "lookup"]
