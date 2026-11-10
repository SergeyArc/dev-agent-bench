"""Idempotency table of the consumer."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("processed_messages", sa.Column("message_id", sa.Text(), primary_key=True))


def downgrade() -> None:
    op.drop_table("processed_messages")
