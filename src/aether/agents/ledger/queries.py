"""``LedgerQueries``: LEDGER's read-only answers to the spec's operational-visibility questions.

Pure repository reads returning frozen view models. Nothing here writes, calls a tool or an LLM,
so other agents and the API can share one instance safely.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aether.agents.ledger.workflow import verdict_for_status
from aether.communication.contracts import VerificationVerdict
from aether.storage.models import (
    AuthorizationRequestRecord,
    AuthorizationStatus,
    CapabilityRecord,
    ErrorRecord,
    ExecutionRunRecord,
    InstallationRecord,
    RecoveryAttemptRecord,
    RequirementRecord,
    RequirementStatus,
    RunStatus,
    SecurityDecisionRecord,
    TaskDependencyRecord,
    TaskRecord,
    TaskStatus,
    TestResultRecord,
    TestRunRecord,
)

DEFAULT_LIMIT = 100
ACTIVE_TASK_STATES = frozenset(
    {
        TaskStatus.READY,
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_FOR_APPROVAL,
        TaskStatus.WAITING_FOR_VERIFICATION,
        TaskStatus.BLOCKED,
        TaskStatus.PAUSED,
        TaskStatus.RETRYING,
    }
)
_FINISHED_TASK_STATES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED})


class LedgerView(BaseModel):
    """Frozen, detached copy of a record; naive SQLite timestamps are read as UTC."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    @field_validator("*", mode="after")
    @classmethod
    def _utc(cls, value: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @classmethod
    def of(cls, record: object) -> Self:
        return cls.model_validate(record, from_attributes=True)


class TaskView(LedgerView):
    __test__ = False

    id: UUID
    objective_id: UUID
    requirement_id: UUID | None
    title: str
    state: str
    assigned_agent: str | None
    priority: int
    attempt: int
    max_attempts: int
    last_error: str | None
    updated_at: datetime


class RequirementView(LedgerView):
    id: UUID
    objective_id: UUID
    task_id: UUID | None
    title: str
    requirement_type: str
    status: str
    priority: int
    acceptance_criteria: tuple[str, ...]
    security_review_required: bool
    created_by: str
    updated_at: datetime


class SecurityReviewView(LedgerView):
    id: UUID
    task_id: UUID | None
    decision: str
    operation: str
    input_hash: str
    rationale: str
    scope: dict[str, Any]
    expires_at: datetime | None
    created_at: datetime

    @property
    def restrictions(self) -> tuple[str, ...]:
        return tuple(self.scope.get("restrictions") or ())

    @property
    def risks(self) -> tuple[str, ...]:
        return tuple(self.scope.get("risks") or ())

    @property
    def requested_by(self) -> str | None:
        return self.scope.get("requested_by")


class AuthorizationView(LedgerView):
    id: UUID
    objective_id: UUID | None
    task_id: UUID | None
    security_decision_id: UUID | None
    question: str
    input_hash: str | None
    status: str
    requested_at: datetime
    responded_at: datetime | None
    expires_at: datetime | None


class InstallationView(LedgerView):
    id: UUID
    capability_id: UUID | None
    name: str
    version: str | None
    source: str
    integrity_hash: str | None
    status: str
    security_decision_id: UUID | None
    created_at: datetime
    completed_at: datetime | None


class ExecutionRunView(LedgerView):
    id: UUID
    task_id: UUID | None
    agent: str
    operation: str
    status: str
    exit_code: int | None
    security_decision_id: UUID | None
    started_at: datetime | None
    finished_at: datetime | None


class TestRunView(LedgerView):
    __test__ = False

    id: UUID
    task_id: UUID | None
    capability_id: UUID | None
    execution_run_id: UUID | None
    suite: str
    status: str
    passed: int
    failed: int
    errors: int
    acceptance_results: tuple[dict[str, Any], ...]
    report_path: str | None
    started_at: datetime
    finished_at: datetime | None
    stored_verdict: str | None = Field(default=None, validation_alias="verdict")

    @property
    def verdict(self) -> VerificationVerdict | None:
        if self.stored_verdict:
            return VerificationVerdict(self.stored_verdict)
        return verdict_for_status(self.status)


class TestResultView(LedgerView):
    __test__ = False

    test_run_id: UUID
    name: str
    outcome: str
    duration_ms: int | None
    message: str | None


class CapabilityView(LedgerView):
    id: UUID
    name: str
    lifecycle_status: str
    verification_status: str
    provider: str | None
    version: str | None
    requirement_id: UUID | None
    objective_id: UUID | None
    known_limitations: tuple[str, ...]
    updated_at: datetime


class ErrorView(LedgerView):
    id: UUID
    error_type: str
    message: str
    agent: str | None
    aggregate_type: str | None
    aggregate_id: UUID | None
    details: dict[str, Any]
    resolved: bool
    occurred_at: datetime


class RecoveryAttemptView(LedgerView):
    id: UUID
    error_id: UUID
    action: str
    outcome: str
    details: dict[str, Any]
    attempted_at: datetime


class LedgerSnapshot(BaseModel):
    """Counts answering "what is the system doing right now" in one read."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    tasks_by_state: dict[str, int]
    agent_workload: dict[str, dict[str, int]]
    blocked_tasks: int
    retrying_tasks: int
    requirements_awaiting_evaluation: int
    eligible_requirements: int
    pending_authorizations: int
    capability_states: dict[str, int]
    open_test_runs: int
    unresolved_errors: int


class LedgerQueries:
    """Deterministic, read-only operational queries over AETHER's records."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def _all(self, statement: Select[Any]) -> list[Any]:
        async with self._sessions() as session:
            return list(await session.scalars(statement))

    # -- tasks and agents --------------------------------------------------------------------

    async def tasks(
        self,
        *,
        state: TaskStatus | str | None = None,
        agent: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[TaskView]:
        statement = select(TaskRecord)
        if state is not None:
            statement = statement.where(TaskRecord.state == TaskStatus(state).value)
        if agent is not None:
            statement = statement.where(TaskRecord.assigned_agent == agent)
        ordering = (TaskRecord.priority.desc(), TaskRecord.created_at)
        return [TaskView.of(r) for r in await self._all(statement.order_by(*ordering).limit(limit))]

    async def work_assignments(self) -> dict[str, list[TaskView]]:
        """Who works on what: active tasks grouped by assigned agent (``unassigned`` if none)."""
        states = [state.value for state in ACTIVE_TASK_STATES]
        statement = (
            select(TaskRecord)
            .where(TaskRecord.state.in_(states))
            .order_by(TaskRecord.priority.desc(), TaskRecord.created_at)
        )
        grouped: dict[str, list[TaskView]] = defaultdict(list)
        for record in await self._all(statement):
            grouped[record.assigned_agent or "unassigned"].append(TaskView.of(record))
        return dict(grouped)

    async def agent_workload(self) -> dict[str, dict[str, int]]:
        """Non-finished task counts per agent and state."""
        statement = select(TaskRecord.assigned_agent, TaskRecord.state)
        workload: dict[str, Counter[str]] = defaultdict(Counter)
        async with self._sessions() as session:
            for agent, state in await session.execute(statement):
                if TaskStatus(state) not in _FINISHED_TASK_STATES:
                    workload[agent or "unassigned"][state] += 1
        return {agent: dict(counts) for agent, counts in workload.items()}

    async def tasks_by_state(self) -> dict[str, int]:
        async with self._sessions() as session:
            states = list(await session.scalars(select(TaskRecord.state)))
        return dict(Counter(states))

    async def blocked_tasks(self) -> list[TaskView]:
        return await self.tasks(state=TaskStatus.BLOCKED)

    async def retrying_tasks(self) -> list[TaskView]:
        """Tasks being retried, or still open after at least one failed attempt."""
        finished = [state.value for state in _FINISHED_TASK_STATES]
        statement = (
            select(TaskRecord)
            .where(
                (TaskRecord.state == TaskStatus.RETRYING.value)
                | ((TaskRecord.attempt > 0) & TaskRecord.state.not_in(finished))
            )
            .order_by(TaskRecord.updated_at)
        )
        return [TaskView.of(record) for record in await self._all(statement)]

    async def ready_tasks(self) -> list[TaskView]:
        """Pending or ready tasks whose every dependency has completed."""
        open_states = (TaskStatus.PENDING.value, TaskStatus.READY.value)
        async with self._sessions() as session:
            candidates = list(
                await session.scalars(
                    select(TaskRecord)
                    .where(TaskRecord.state.in_(open_states))
                    .order_by(TaskRecord.priority.desc(), TaskRecord.created_at)
                )
            )
            unmet = set(
                await session.scalars(
                    select(TaskDependencyRecord.task_id)
                    .join(TaskRecord, TaskRecord.id == TaskDependencyRecord.depends_on_task_id)
                    .where(TaskRecord.state != TaskStatus.COMPLETED.value)
                )
            )
        return [TaskView.of(record) for record in candidates if record.id not in unmet]

    # -- requirements ------------------------------------------------------------------------

    async def requirements(
        self, *, status: RequirementStatus | str | None = None, objective_id: UUID | None = None
    ) -> list[RequirementView]:
        statement = select(RequirementRecord)
        if status is not None:
            statement = statement.where(RequirementRecord.status == RequirementStatus(status).value)
        if objective_id is not None:
            statement = statement.where(RequirementRecord.objective_id == objective_id)
        ordering = (RequirementRecord.priority.desc(), RequirementRecord.created_at)
        return [RequirementView.of(r) for r in await self._all(statement.order_by(*ordering))]

    async def requirements_awaiting_evaluation(self) -> list[RequirementView]:
        return await self.requirements(status=RequirementStatus.PROPOSED)

    async def eligible_requirements(self) -> list[RequirementView]:
        """Approved, testable requirements that no one has started yet (highest priority first)."""
        return [
            requirement
            for requirement in await self.requirements(status=RequirementStatus.APPROVED)
            if requirement.acceptance_criteria
        ]

    # -- security ----------------------------------------------------------------------------

    async def security_reviews(
        self,
        *,
        task_id: UUID | None = None,
        input_hash: str | None = None,
        decision: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[SecurityReviewView]:
        statement = select(SecurityDecisionRecord)
        if task_id is not None:
            statement = statement.where(SecurityDecisionRecord.task_id == task_id)
        if input_hash is not None:
            statement = statement.where(SecurityDecisionRecord.input_hash == input_hash)
        if decision is not None:
            statement = statement.where(SecurityDecisionRecord.decision == decision)
        statement = statement.order_by(SecurityDecisionRecord.created_at.desc()).limit(limit)
        return [SecurityReviewView.of(record) for record in await self._all(statement)]

    async def pending_authorizations(self) -> list[AuthorizationView]:
        """User authorizations still awaiting an explicit answer and not yet expired."""
        now = self._clock()
        statement = (
            select(AuthorizationRequestRecord)
            .where(AuthorizationRequestRecord.status == AuthorizationStatus.PENDING.value)
            .order_by(AuthorizationRequestRecord.requested_at)
        )
        views = [AuthorizationView.of(record) for record in await self._all(statement)]
        return [view for view in views if view.expires_at is None or view.expires_at > now]

    # -- dependencies, builds and tests ------------------------------------------------------

    async def dependencies(
        self, *, status: RunStatus | str | None = None, capability_id: UUID | None = None
    ) -> list[InstallationView]:
        """Proposed (pending), installed (succeeded) and failed dependency installations."""
        statement = select(InstallationRecord)
        if status is not None:
            statement = statement.where(InstallationRecord.status == RunStatus(status).value)
        if capability_id is not None:
            statement = statement.where(InstallationRecord.capability_id == capability_id)
        statement = statement.order_by(InstallationRecord.created_at)
        return [InstallationView.of(record) for record in await self._all(statement)]

    async def execution_runs(
        self,
        *,
        task_id: UUID | None = None,
        agent: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[ExecutionRunView]:
        statement = select(ExecutionRunRecord)
        if task_id is not None:
            statement = statement.where(ExecutionRunRecord.task_id == task_id)
        if agent is not None:
            statement = statement.where(ExecutionRunRecord.agent == agent)
        statement = statement.order_by(ExecutionRunRecord.started_at.desc()).limit(limit)
        return [ExecutionRunView.of(record) for record in await self._all(statement)]

    async def test_runs(
        self,
        *,
        capability_id: UUID | None = None,
        task_id: UUID | None = None,
        status: RunStatus | str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[TestRunView]:
        statement = select(TestRunRecord)
        if capability_id is not None:
            statement = statement.where(TestRunRecord.capability_id == capability_id)
        if task_id is not None:
            statement = statement.where(TestRunRecord.task_id == task_id)
        if status is not None:
            statement = statement.where(TestRunRecord.status == RunStatus(status).value)
        statement = statement.order_by(TestRunRecord.started_at.desc()).limit(limit)
        return [TestRunView.of(record) for record in await self._all(statement)]

    async def test_results(self, test_run_id: UUID) -> list[TestResultView]:
        statement = (
            select(TestResultRecord)
            .where(TestResultRecord.test_run_id == test_run_id)
            .order_by(TestResultRecord.name)
        )
        return [TestResultView.of(record) for record in await self._all(statement)]

    # -- capabilities ------------------------------------------------------------------------

    async def capabilities(self, *, status: str | None = None) -> list[CapabilityView]:
        statement = select(CapabilityRecord)
        if status is not None:
            statement = statement.where(CapabilityRecord.lifecycle_status == status)
        statement = statement.order_by(CapabilityRecord.name)
        return [CapabilityView.of(record) for record in await self._all(statement)]

    async def capability(self, capability_id: UUID) -> CapabilityView | None:
        async with self._sessions() as session:
            record = await session.get(CapabilityRecord, capability_id)
        return CapabilityView.of(record) if record is not None else None

    async def capability_states(self) -> dict[str, int]:
        async with self._sessions() as session:
            states = list(await session.scalars(select(CapabilityRecord.lifecycle_status)))
        return dict(Counter(states))

    # -- errors and recovery -----------------------------------------------------------------

    async def unresolved_errors(
        self, *, error_types: Iterable[str] | None = None, aggregate_id: UUID | None = None
    ) -> list[ErrorView]:
        statement = select(ErrorRecord).where(ErrorRecord.resolved.is_(False))
        if error_types is not None:
            statement = statement.where(ErrorRecord.error_type.in_(list(error_types)))
        if aggregate_id is not None:
            statement = statement.where(ErrorRecord.aggregate_id == aggregate_id)
        statement = statement.order_by(ErrorRecord.occurred_at)
        return [ErrorView.of(record) for record in await self._all(statement)]

    async def errors(
        self, *, aggregate_id: UUID | None = None, limit: int = DEFAULT_LIMIT
    ) -> list[ErrorView]:
        statement = select(ErrorRecord)
        if aggregate_id is not None:
            statement = statement.where(ErrorRecord.aggregate_id == aggregate_id)
        statement = statement.order_by(ErrorRecord.occurred_at.desc()).limit(limit)
        return [ErrorView.of(record) for record in await self._all(statement)]

    async def recovery_attempts(self, error_id: UUID | None = None) -> list[RecoveryAttemptView]:
        statement = select(RecoveryAttemptRecord)
        if error_id is not None:
            statement = statement.where(RecoveryAttemptRecord.error_id == error_id)
        statement = statement.order_by(RecoveryAttemptRecord.attempted_at)
        return [RecoveryAttemptView.of(record) for record in await self._all(statement)]

    # -- summary -----------------------------------------------------------------------------

    async def snapshot(self) -> LedgerSnapshot:
        open_runs = await self._all(
            select(TestRunRecord.id).where(
                TestRunRecord.status.in_((RunStatus.PENDING.value, RunStatus.RUNNING.value))
            )
        )
        return LedgerSnapshot(
            generated_at=self._clock(),
            tasks_by_state=await self.tasks_by_state(),
            agent_workload=await self.agent_workload(),
            blocked_tasks=len(await self.blocked_tasks()),
            retrying_tasks=len(await self.retrying_tasks()),
            requirements_awaiting_evaluation=len(await self.requirements_awaiting_evaluation()),
            eligible_requirements=len(await self.eligible_requirements()),
            pending_authorizations=len(await self.pending_authorizations()),
            capability_states=await self.capability_states(),
            open_test_runs=len(open_runs),
            unresolved_errors=len(await self.unresolved_errors()),
        )
