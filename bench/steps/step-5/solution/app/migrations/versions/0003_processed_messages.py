"""Record of processed messages.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("message_id", name="processed_messages_pkey"),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
