"""Track which consumer claimed a message and until when, and store inconclusive runs as such.

Message claims let a starting engine requeue only messages whose consumer is gone. In-flight
messages written before this revision have no claim and count as expired. Test runs with an
inconclusive verdict used to be stored as ``cancelled``; they now carry the ``inconclusive``
status. ``waiting_for_verification`` needs no schema change (task states are plain strings).

Revision ID: 0006_claims_and_run_status
Revises: 0005_provider_usage_objective
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_claims_and_run_status"
down_revision = "0005_provider_usage_objective"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        "UPDATE test_runs SET status = 'inconclusive' "
        "WHERE verdict = 'inconclusive' AND status = 'cancelled'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE test_runs SET status = 'cancelled' "
        "WHERE verdict = 'inconclusive' AND status = 'inconclusive'"
    )
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("claim_expires_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claimed_by")
