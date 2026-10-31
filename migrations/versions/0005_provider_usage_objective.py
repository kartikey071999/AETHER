"""Attribute provider usage to an objective so per-objective LLM budgets survive restarts.

Existing rows keep a NULL objective_id; only new calls are attributed.

Revision ID: 0005_provider_usage_objective
Revises: 0004_test_run_verdict
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_provider_usage_objective"
down_revision = "0004_test_run_verdict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_usage") as batch_op:
        batch_op.add_column(sa.Column("objective_id", sa.Uuid(), nullable=True))
        batch_op.create_index("ix_provider_usage_objective_agent", ["objective_id", "agent"])


def downgrade() -> None:
    with op.batch_alter_table("provider_usage") as batch_op:
        batch_op.drop_index("ix_provider_usage_objective_agent")
        batch_op.drop_column("objective_id")
