"""Agent-authored workflows follow a controlled lifecycle before anything can run them.

    proposed --(WARDEN approves; automatic when no step has side effects)--> approved
    approved --(LEDGER dry-run on fixtures passes)--> verified          (only verified runs)
    proposed/approved --(WARDEN or LEDGER rejects with a reason)--> rejected
    verified --(owner or WARDEN)--> disabled

Agents may only author workflows in their own namespace (``<agent>.<name>``), using skills they
own. A workflow is data: it cannot grant permissions or change policy; every step still runs
through the author's own facades and the policy layer.
"""

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from aether.communication.messages import AgentId
from aether.skills.base import SkillContext
from aether.storage.models import WorkflowDefinitionRecord, WorkflowStatus
from aether.workflows.definition import WorkflowDefinition, WorkflowDefinitionError
from aether.workflows.engine import WorkflowEngine, WorkflowNotFound

_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.PROPOSED: frozenset({WorkflowStatus.APPROVED, WorkflowStatus.REJECTED}),
    WorkflowStatus.APPROVED: frozenset({WorkflowStatus.VERIFIED, WorkflowStatus.REJECTED}),
    WorkflowStatus.VERIFIED: frozenset({WorkflowStatus.DISABLED}),
    WorkflowStatus.REJECTED: frozenset(),
    WorkflowStatus.DISABLED: frozenset(),
}


class WorkflowLifecycleError(PermissionError):
    pass


def namespace(agent: AgentId) -> str:
    return f"{agent.value.lower()}."


class WorkflowLifecycle:
    def __init__(self, engine: WorkflowEngine) -> None:
        self.engine = engine

    async def propose(
        self, author: AgentId, definition: dict[str, Any], rationale: str
    ) -> tuple[str, int, str]:
        """Validate and store a proposal; returns (name, version, status)."""
        if not rationale.strip():
            raise WorkflowDefinitionError("a workflow proposal needs a rationale")
        data = dict(definition)
        data["owner"] = data.get("owner", author.value)
        if str(data["owner"]).upper() != author.value:
            raise WorkflowLifecycleError(f"{author.value} may only propose its own workflows")
        name = str(data.get("name", ""))
        if not name.startswith(namespace(author)):
            raise WorkflowLifecycleError(f"workflow names must start with {namespace(author)!r}")
        data["version"] = await self._next_version(name)
        try:
            proposal = WorkflowDefinition.model_validate(data)
        except ValidationError as error:
            # Precise messages let the authoring agent fix its proposal; no input data is echoed.
            details = "; ".join(item["msg"] for item in error.errors(include_input=False)[:3])
            raise WorkflowDefinitionError(f"invalid workflow: {details}") from None
        proposal.validate_skills(self.engine.skills)
        side_effects = any(
            self.engine.skills.get(step.skill).side_effects for step in proposal.steps
        )
        status = WorkflowStatus.PROPOSED if side_effects else WorkflowStatus.APPROVED
        await self.engine.register(proposal, status=status, built_in=False, created_by=author.value)
        await self._event(proposal.name, proposal.version, author, None, status, rationale[:500])
        return proposal.name, proposal.version, str(status)

    async def approve(self, reviewer: AgentId, name: str, version: int, rationale: str) -> None:
        if reviewer is not AgentId.WARDEN:
            raise WorkflowLifecycleError("only WARDEN may approve workflows with side effects")
        await self._transition(reviewer, name, version, WorkflowStatus.APPROVED, rationale)

    async def reject(self, reviewer: AgentId, name: str, version: int, reason: str) -> None:
        if reviewer not in {AgentId.WARDEN, AgentId.LEDGER}:
            raise WorkflowLifecycleError("only WARDEN or LEDGER may reject workflows")
        if not reason.strip():
            raise WorkflowDefinitionError("a rejection needs a reason")
        await self._transition(reviewer, name, version, WorkflowStatus.REJECTED, reason)

    async def verify(
        self,
        verifier: AgentId,
        name: str,
        version: int,
        *,
        inputs: dict[str, Any],
        fixtures: dict[str, dict[str, Any]],
        ctx: SkillContext,
    ) -> bool:
        """LEDGER dry-runs the workflow on fixtures (as its owner, no side effects)."""
        if verifier is not AgentId.LEDGER:
            raise WorkflowLifecycleError("only LEDGER may verify workflows")
        definition, status = await self.engine.load(name, version)
        if status != WorkflowStatus.APPROVED:
            raise WorkflowLifecycleError(f"{name} v{version} is {status}, not approved")
        dry = SkillContext(
            agent_id=definition.owner,
            agent_ctx=ctx.agent_ctx,
            objective_id=ctx.objective_id,
            task_id=ctx.task_id,
            correlation_id=ctx.correlation_id,
            dry_run=True,
            fixtures=fixtures,
        )
        try:
            result = await self.engine.run(definition, inputs, dry)
        except WorkflowDefinitionError as error:
            await self.reject(verifier, name, version, f"dry run could not start: {error}")
            return False
        if result.status != "succeeded":
            await self.reject(verifier, name, version, f"dry run failed: {result.error}")
            return False
        await self._transition(
            verifier, name, version, WorkflowStatus.VERIFIED, f"dry run {result.run_id} passed"
        )
        return True

    async def disable(self, actor: AgentId, name: str, version: int, reason: str) -> None:
        definition, _ = await self.engine.load(name, version)
        if actor not in {definition.owner, AgentId.WARDEN}:
            raise WorkflowLifecycleError("only the owner or WARDEN may disable a workflow")
        await self._transition(actor, name, version, WorkflowStatus.DISABLED, reason)

    async def _transition(
        self, actor: AgentId, name: str, version: int, target: WorkflowStatus, rationale: str
    ) -> None:
        async with self.engine.sessions() as session, session.begin():
            record = await session.scalar(
                select(WorkflowDefinitionRecord)
                .where(
                    WorkflowDefinitionRecord.name == name,
                    WorkflowDefinitionRecord.version == version,
                )
                .with_for_update()
            )
            if record is None:
                raise WorkflowNotFound(f"unknown workflow: {name} v{version}")
            if record.built_in:
                raise WorkflowLifecycleError("built-in workflows cannot change status")
            current = WorkflowStatus(record.status)
            if target not in _TRANSITIONS[current]:
                raise WorkflowLifecycleError(
                    f"cannot move {name} v{version} from {current} to {target}"
                )
            record.status = target
            record.updated_at = self.engine.clock()
            self.engine._event(
                session,
                "workflow.definition_status_changed",
                actor.value,
                record.id,
                {
                    "name": name,
                    "version": version,
                    "from_status": str(current),
                    "to_status": str(target),
                    "actor": actor.value,
                    "rationale": rationale[:500],
                },
            )

    async def _event(self, name, version, actor, current, target, rationale) -> None:
        async with self.engine.sessions() as session, session.begin():
            record = await session.scalar(
                select(WorkflowDefinitionRecord).where(
                    WorkflowDefinitionRecord.name == name,
                    WorkflowDefinitionRecord.version == version,
                )
            )
            self.engine._event(
                session,
                "workflow.definition_status_changed",
                actor.value,
                record.id,
                {
                    "name": name,
                    "version": version,
                    "from_status": str(current) if current else None,
                    "to_status": str(target),
                    "actor": actor.value,
                    "rationale": rationale,
                },
            )

    async def _next_version(self, name: str) -> int:
        async with self.engine.sessions() as session:
            latest = await session.scalar(
                select(WorkflowDefinitionRecord.version)
                .where(WorkflowDefinitionRecord.name == name)
                .order_by(WorkflowDefinitionRecord.version.desc())
                .limit(1)
            )
        return (latest or 0) + 1
