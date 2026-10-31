"""Persist independent test-run verdicts without changing existing run statuses.

Revision ID: 0004_test_run_verdict
Revises: 0003_event_session_and_auth_hash
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_test_run_verdict"
down_revision = "0003_event_session_and_auth_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("test_runs") as batch_op:
        batch_op.add_column(sa.Column("verdict", sa.String(length=24), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("test_runs") as batch_op:
        batch_op.drop_column("verdict")
