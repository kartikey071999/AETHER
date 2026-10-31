"""Versioned workflow definitions, workflow runs and resumable per-step state.

Workflows are declarative DAGs of skill steps executed by the runtime workflow engine (code, not
an agent's model). Built-in workflows and agent-authored ones share ``workflow_definitions``;
``workflow_step_runs`` lets an interrupted run resume after its last finished step.

Revision ID: 0007_skills_workflows
Revises: 0006_claims_and_run_status
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_skills_workflows"
down_revision = "0006_claims_and_run_status"
branch_labels = None
depends_on = None

JSON_DOCUMENT = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner_agent", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("built_in", sa.Boolean(), nullable=False),
        sa.Column("definition", JSON_DOCUMENT, nullable=False),
        sa.Column("definition_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_workflow_definitions_name_version"),
    )
    op.create_index(
        "ix_workflow_definitions_owner_status",
        "workflow_definitions",
        ["owner_agent", "status"],
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workflow_name", sa.String(length=120), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=24), nullable=False),
        sa.Column("objective_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("inputs", JSON_DOCUMENT, nullable=False),
        sa.Column("outputs", JSON_DOCUMENT, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_workflow_runs_idempotency_key"),
    )
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index("ix_workflow_runs_task_id", "workflow_runs", ["task_id"])
    op.create_table(
        "workflow_step_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("skill", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("output", JSON_DOCUMENT, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "step_id", name="uq_workflow_step_runs_run_step"),
        sa.CheckConstraint("attempts >= 0", name="ck_workflow_step_runs_attempts"),
    )


def downgrade() -> None:
    op.drop_table("workflow_step_runs")
    op.drop_index("ix_workflow_runs_task_id", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_status", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_workflow_definitions_owner_status", table_name="workflow_definitions")
    op.drop_table("workflow_definitions")
