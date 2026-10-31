"""Relational records for AETHER's durable operational state."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in_check(column: str, values: type[StrEnum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({allowed})"


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_VERIFICATION = "waiting_for_verification"
    BLOCKED = "blocked"
    PAUSED = "paused"
    RETRYING = "retrying"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RequirementStatus(StrEnum):
    PROPOSED = "proposed"
    EVALUATING = "evaluating"
    NEEDS_EVIDENCE = "needs_evidence"
    APPROVED = "approved"
    IMPLEMENTED = "implemented"
    VERIFIED = "verified"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    SATISFIED = "satisfied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EventCategory(StrEnum):
    GENERAL = "general"
    OBJECTIVE = "objective"
    TASK = "task"
    REQUIREMENT = "requirement"
    RESEARCH = "research"
    CAPABILITY = "capability"
    AGENT = "agent"
    ERROR = "error"
    AUTHORIZATION = "authorization"
    PERSONALIZATION = "personalization"
    KNOWLEDGE = "knowledge"
    PROVIDER = "provider"
    TEST = "test"


class AgentHealthStatus(StrEnum):
    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


class FindingClassification(StrEnum):
    VERIFIED = "verified"
    HYPOTHESIS = "hypothesis"
    ASSUMPTION = "assumption"
    UNTESTED = "untested"


class FindingStatus(StrEnum):
    SUBMITTED = "submitted"
    CONSOLIDATED = "consolidated"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    NEEDS_EVIDENCE = "needs_evidence"


class CapabilityStatus(StrEnum):
    DISCOVERED = "discovered"
    PROPOSED = "proposed"
    APPROVED = "approved"
    INSTALLED = "installed"
    TESTED = "tested"
    VERIFIED = "verified"
    DISABLED = "disabled"
    DEPRECATED = "deprecated"


class AuthorizationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class PreferenceSource(StrEnum):
    EXPLICIT = "explicit"
    FEEDBACK = "feedback"


class InteractionDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class RunStatus(StrEnum):
    """Lifecycle of installations, execution runs, and test runs."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # A test run that could not reach a verdict either way (not a cancellation).
    INCONCLUSIVE = "inconclusive"


class ResultOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestVerdict(StrEnum):
    __test__ = False

    VERIFIED = "verified"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ObjectiveRecord(Base):
    __tablename__ = "objectives"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    rationale: Mapped[str | None] = mapped_column(Text)
    completion_criteria: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    created_by: Mapped[str] = mapped_column(String(24), nullable=False, default="ORCHESTR")
    parent_objective_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="SET NULL", name="fk_objectives_parent_objective_id")
    )
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_benefit: Mapped[str | None] = mapped_column(Text)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    stopping_conditions: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list, server_default=text("'[]'")
    )
    generated_by_agent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    base_priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    last_priority_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    tasks: Mapped[list["TaskRecord"]] = relationship(back_populates="objective")


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("priority >= 0 AND priority <= 100", name="ck_tasks_priority"),
        CheckConstraint("attempt >= 0 AND max_attempts >= 0", name="ck_tasks_attempts"),
        Index("ix_tasks_state_priority", "state", "priority"),
        Index("ix_tasks_lease_expires_at", "lease_expires_at"),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    objective_id: Mapped[UUID] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT"), nullable=False
    )
    parent_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_tasks_parent_task_id")
    )
    # use_alter breaks the tasks <-> requirements foreign-key cycle for create_all.
    requirement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "requirements.id",
            ondelete="SET NULL",
            name="fk_tasks_requirement_id",
            use_alter=True,
        )
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=TaskStatus.PENDING)
    assigned_agent: Mapped[str | None] = mapped_column(String(24))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    estimated_ram_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_disk_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    interruptible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    estimated_cpu_percent: Mapped[float | None] = mapped_column(Float)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    last_error: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    objective: Mapped[ObjectiveRecord] = relationship(back_populates="tasks")


class RequirementRecord(Base):
    __tablename__ = "requirements"
    __table_args__ = (Index("ix_requirements_status", "status"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    objective_id: Mapped[UUID] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requirement_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    acceptance_criteria: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    source_finding_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "research_findings.id",
            ondelete="SET NULL",
            name="fk_requirements_source_finding_id",
        )
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    security_review_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_by: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class MessageRecord(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_target_status", "target_agent", "status"),
        Index("ix_messages_correlation_id", "correlation_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_agent: Mapped[str] = mapped_column(String(24), nullable=False)
    target_agent: Mapped[str] = mapped_column(String(24), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    objective_id: Mapped[UUID | None] = mapped_column(ForeignKey("objectives.id"))
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id"))
    requirement_id: Mapped[UUID | None] = mapped_column(ForeignKey("requirements.id"))
    correlation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requires_response: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A live consumer keeps pushing this forward; once it is past, the claim may be taken over.
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
        Index("ix_events_aggregate", "aggregate_type", "aggregate_id", "created_at"),
        Index("ix_events_category", "category"),
        Index("ix_events_correlation_id", "correlation_id"),
        Index("ix_events_objective_id", "objective_id"),
        Index("ix_events_task_id", "task_id"),
        Index("ix_events_session_id", "session_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=EventCategory.GENERAL,
        server_default=EventCategory.GENERAL.value,
    )
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str | None] = mapped_column(String(64))
    # Plain columns (no foreign keys) so the audit trail never blocks deletes.
    correlation_id: Mapped[UUID | None] = mapped_column(Uuid)
    objective_id: Mapped[UUID | None] = mapped_column(Uuid)
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    session_id: Mapped[str | None] = mapped_column(String(100))
    branch: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class SecurityDecisionRecord(Base):
    __tablename__ = "security_decisions"
    __table_args__ = (Index("ix_security_decisions_task", "task_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    operation: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON_DOCUMENT, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ResourceMeasurementRecord(Base):
    __tablename__ = "resource_measurements"
    __table_args__ = (Index("ix_resource_measurements_observed_at", "observed_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    available_ram_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    free_disk_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentRecord(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    responsibility: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    model_preference: Mapped[str | None] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AgentHeartbeatRecord(Base):
    __tablename__ = "agent_heartbeats"
    __table_args__ = (Index("ix_agent_heartbeats_agent_observed", "agent_id", "observed_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.agent_id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class TaskDependencyRecord(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "depends_on_task_id", name="pk_task_dependencies"),
        CheckConstraint("task_id <> depends_on_task_id", name="ck_task_dependencies_not_self"),
        Index("ix_task_dependencies_depends_on", "depends_on_task_id"),
    )

    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    depends_on_task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ResearchFindingRecord(Base):
    __tablename__ = "research_findings"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_research_findings_confidence"
        ),
        CheckConstraint(
            _in_check("classification", FindingClassification),
            name="ck_research_findings_classification",
        ),
        CheckConstraint(_in_check("status", FindingStatus), name="ck_research_findings_status"),
        Index("ix_research_findings_status", "status"),
        Index("ix_research_findings_objective_id", "objective_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    objective_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(String(24), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    findings: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_approaches: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    recommendation: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    classification: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=FindingStatus.SUBMITTED)
    rationale: Mapped[str | None] = mapped_column(Text)
    consolidated_by: Mapped[str | None] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class FindingEvidenceRecord(Base):
    __tablename__ = "finding_evidence"
    __table_args__ = (Index("ix_finding_evidence_finding_id", "finding_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    finding_id: Mapped[UUID] = mapped_column(
        ForeignKey("research_findings.id", ondelete="CASCADE"), nullable=False
    )
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str | None] = mapped_column(String(128))
    trust_level: Mapped[str] = mapped_column(String(24), nullable=False, default="untrusted")
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class CapabilityRecord(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("name", name="uq_capabilities_name"),
        CheckConstraint(
            _in_check("lifecycle_status", CapabilityStatus),
            name="ck_capabilities_lifecycle_status",
        ),
        Index("ix_capabilities_lifecycle_status", "lifecycle_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    lifecycle_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=CapabilityStatus.DISCOVERED
    )
    provider: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(64))
    entrypoint: Mapped[str | None] = mapped_column(String(500))
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    required_permissions: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    dependencies: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    resource_estimates: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    verification_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unverified"
    )
    known_limitations: Mapped[list[str]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    requirement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("requirements.id", ondelete="SET NULL")
    )
    objective_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class InstallationRecord(Base):
    __tablename__ = "installations"
    __table_args__ = (Index("ix_installations_capability_id", "capability_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    capability_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("capabilities.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    integrity_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.PENDING)
    command: Mapped[str | None] = mapped_column(Text)
    security_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("security_decisions.id", ondelete="SET NULL")
    )
    artifact_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExecutionRunRecord(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (Index("ix_execution_runs_task_started", "task_id", "started_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(String(24), nullable=False)
    operation: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.PENDING)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout_path: Mapped[str | None] = mapped_column(String(1024))
    stderr_path: Mapped[str | None] = mapped_column(String(1024))
    security_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("security_decisions.id", ondelete="SET NULL")
    )
    resource_usage: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestRunRecord(Base):
    __test__ = False
    __tablename__ = "test_runs"
    __table_args__ = (
        CheckConstraint("passed >= 0 AND failed >= 0 AND errors >= 0", name="ck_test_runs_counts"),
        Index("ix_test_runs_capability_id", "capability_id"),
        Index("ix_test_runs_task_id", "task_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    capability_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("capabilities.id", ondelete="SET NULL")
    )
    execution_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("execution_runs.id", ondelete="SET NULL")
    )
    suite: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.PENDING)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acceptance_results: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=list
    )
    report_path: Mapped[str | None] = mapped_column(String(1024))
    verdict: Mapped[str | None] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestResultRecord(Base):
    __test__ = False
    __tablename__ = "test_results"
    __table_args__ = (
        CheckConstraint(_in_check("outcome", ResultOutcome), name="ck_test_results_outcome"),
        Index("ix_test_results_test_run_id", "test_run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    test_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)


class ErrorRecord(Base):
    __tablename__ = "errors"
    __table_args__ = (
        Index("ix_errors_resolved_occurred", "resolved", "occurred_at"),
        Index("ix_errors_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    aggregate_type: Mapped[str | None] = mapped_column(String(32))
    aggregate_id: Mapped[UUID | None] = mapped_column(Uuid)
    agent: Mapped[str | None] = mapped_column(String(24))
    error_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class RecoveryAttemptRecord(Base):
    __tablename__ = "recovery_attempts"
    __table_args__ = (Index("ix_recovery_attempts_error_id", "error_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    error_id: Mapped[UUID] = mapped_column(
        ForeignKey("errors.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class AuthorizationRequestRecord(Base):
    __tablename__ = "authorization_requests"
    __table_args__ = (
        CheckConstraint(
            _in_check("status", AuthorizationStatus), name="ck_authorization_requests_status"
        ),
        Index("ix_authorization_requests_status", "status"),
        Index("ix_authorization_requests_input_hash", "input_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    objective_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="SET NULL")
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    security_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("security_decisions.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(128))
    # Silence is never authorization: requests stay pending until explicitly answered.
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=AuthorizationStatus.PENDING,
        server_default=AuthorizationStatus.PENDING.value,
    )
    response: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PersonRecord(Base):
    __tablename__ = "persons"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship: Mapped[str | None] = mapped_column(String(64))
    access_level: Mapped[str] = mapped_column(String(24), nullable=False, default="restricted")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserPreferenceRecord(Base):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("person_id", "key", name="uq_user_preferences_person_key"),
        CheckConstraint(_in_check("source", PreferenceSource), name="ck_user_preferences_source"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    person_id: Mapped[UUID] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[Any] = mapped_column(JSON_DOCUMENT, nullable=False)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default=PreferenceSource.EXPLICIT
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class InteractionRecord(Base):
    __tablename__ = "interactions"
    __table_args__ = (
        CheckConstraint(
            _in_check("direction", InteractionDirection), name="ck_interactions_direction"
        ),
        Index("ix_interactions_person_occurred", "person_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    objective_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="SET NULL")
    )
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    authorization_request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("authorization_requests.id", ondelete="SET NULL")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class KnowledgeEntityRecord(Base):
    __tablename__ = "kg_entities"
    __table_args__ = (UniqueConstraint("entity_type", "name", name="uq_kg_entities_type_name"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class KnowledgeRelationRecord(Base):
    __tablename__ = "kg_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_kg_relations_triple",
        ),
        Index("ix_kg_relations_target", "target_entity_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    source_entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("kg_entities.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ProviderUsageRecord(Base):
    __tablename__ = "provider_usage"
    __table_args__ = (
        CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0",
            name="ck_provider_usage_counts",
        ),
        Index("ix_provider_usage_provider_occurred", "provider", "occurred_at"),
        Index("ix_provider_usage_objective_agent", "objective_id", "agent"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    agent: Mapped[str | None] = mapped_column(String(24))
    # Plain column (no foreign key), like events: usage history must never block objective deletes.
    objective_id: Mapped[UUID | None] = mapped_column(Uuid)
    task_id: Mapped[UUID | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal(0))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="succeeded")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class WorkflowStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    VERIFIED = "verified"
    REJECTED = "rejected"
    DISABLED = "disabled"


class WorkflowDefinitionRecord(Base):
    """A versioned, declarative workflow (a DAG of skill steps); built-in or agent-authored."""

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_workflow_definitions_name_version"),
        Index("ix_workflow_definitions_owner_status", "owner_agent", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_agent: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=WorkflowStatus.PROPOSED)
    built_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class WorkflowRunRecord(Base):
    """One execution of a workflow; step state lives in ``workflow_step_runs``."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_status", "status"),
        Index("ix_workflow_runs_task_id", "task_id"),
        UniqueConstraint("idempotency_key", name="uq_workflow_runs_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workflow_name: Mapped[str] = mapped_column(String(120), nullable=False)
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    agent: Mapped[str] = mapped_column(String(24), nullable=False)
    objective_id: Mapped[UUID | None] = mapped_column(Uuid)
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.PENDING)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowStepRunRecord(Base):
    """Persisted state of one step, so an interrupted run resumes after its last finished step."""

    __tablename__ = "workflow_step_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", name="uq_workflow_step_runs_run_step"),
        CheckConstraint("attempts >= 0", name="ck_workflow_step_runs_attempts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(64), nullable=False)
    skill: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=RunStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
