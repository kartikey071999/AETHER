"""Create the initial persistent runtime schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_runtime_foundation"
down_revision = None
branch_labels = None
depends_on = None

UUID = sa.Uuid(as_uuid=True)
# JSONB on PostgreSQL; plain JSON elsewhere so the migration chain also runs on SQLite in tests.
JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "objectives",
        sa.Column("id", UUID, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("completion_criteria", JSON_DOCUMENT, nullable=False),
        sa.Column("created_by", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", UUID, nullable=False),
        sa.Column("objective_id", UUID, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("assigned_agent", sa.String(length=24), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("estimated_ram_mb", sa.Integer(), nullable=False),
        sa.Column("estimated_disk_mb", sa.Integer(), nullable=False),
        sa.Column("estimated_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("priority >= 0 AND priority <= 100", name="ck_tasks_priority"),
        sa.CheckConstraint("attempt >= 0 AND max_attempts >= 0", name="ck_tasks_attempts"),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_state_priority", "tasks", ["state", "priority"])
    op.create_index("ix_tasks_lease_expires_at", "tasks", ["lease_expires_at"])
    op.create_table(
        "requirements",
        sa.Column("id", UUID, nullable=False),
        sa.Column("objective_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirement_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("acceptance_criteria", JSON_DOCUMENT, nullable=False),
        sa.Column("created_by", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requirements_status", "requirements", ["status"])
    op.create_table(
        "messages",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_agent", sa.String(length=24), nullable=False),
        sa.Column("target_agent", sa.String(length=24), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("objective_id", UUID, nullable=True),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("requirement_id", UUID, nullable=True),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requires_response", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["objective_id"], ["objectives.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_target_status", "messages", ["target_agent", "status"])
    op.create_index("ix_messages_correlation_id", "messages", ["correlation_id"])
    op.create_table(
        "events",
        sa.Column("id", UUID, nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", UUID, nullable=False),
        sa.Column("actor", sa.String(length=24), nullable=False),
        sa.Column("payload", JSON_DOCUMENT, nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_events_idempotency_key"),
    )
    op.create_index(
        "ix_events_aggregate",
        "events",
        ["aggregate_type", "aggregate_id", "created_at"],
    )
    op.create_table(
        "security_decisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("operation", sa.String(length=200), nullable=False),
        sa.Column("scope", JSON_DOCUMENT, nullable=False),
        sa.Column("evidence", JSON_DOCUMENT, nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_decisions_task",
        "security_decisions",
        ["task_id", "created_at"],
    )
    op.create_table(
        "resource_measurements",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("available_ram_bytes", sa.Integer(), nullable=False),
        sa.Column("free_disk_bytes", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_resource_measurements_observed_at",
        "resource_measurements",
        ["observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_resource_measurements_observed_at", table_name="resource_measurements")
    op.drop_table("resource_measurements")
    op.drop_index("ix_security_decisions_task", table_name="security_decisions")
    op.drop_table("security_decisions")
    op.drop_index("ix_events_aggregate", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_messages_correlation_id", table_name="messages")
    op.drop_index("ix_messages_target_status", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_requirements_status", table_name="requirements")
    op.drop_table("requirements")
    op.drop_index("ix_tasks_lease_expires_at", table_name="tasks")
    op.drop_index("ix_tasks_state_priority", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("objectives")
