"""Persist event session/branch and bind user authorization requests to an input hash.

Revision ID: 0003_event_session_and_auth_hash
Revises: 0002_storage_schema_expansion
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_event_session_and_auth_hash"
down_revision = "0002_storage_schema_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(sa.Column("session_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("branch", sa.String(length=200), nullable=True))
        batch_op.create_index("ix_events_session_id", ["session_id"])

    with op.batch_alter_table("authorization_requests") as batch_op:
        batch_op.add_column(sa.Column("input_hash", sa.String(length=128), nullable=True))
        batch_op.create_index("ix_authorization_requests_input_hash", ["input_hash"])


def downgrade() -> None:
    with op.batch_alter_table("authorization_requests") as batch_op:
        batch_op.drop_index("ix_authorization_requests_input_hash")
        batch_op.drop_column("input_hash")

    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_index("ix_events_session_id")
        batch_op.drop_column("branch")
        batch_op.drop_column("session_id")
