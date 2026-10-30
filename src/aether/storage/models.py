"""Relational records for AETHER's durable operational state."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    BLOCKED = "blocked"
    PAUSED = "paused"
    RETRYING = "retrying"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    objective_id: Mapped[UUID] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT"), nullable=False
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
    created_by: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
        Index("ix_events_aggregate", "aggregate_type", "aggregate_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor: Mapped[str] = mapped_column(String(24), nullable=False)
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
    available_ram_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    free_disk_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
