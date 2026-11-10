"""Tracks and their segments (contract 6.2)."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tracks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
    )
    op.create_table(
        "track_segments",
        sa.Column("track_id", sa.Uuid(), sa.ForeignKey("tracks.id"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.UniqueConstraint("track_id", "label"),
    )


def downgrade() -> None:
    op.drop_table("track_segments")
    op.drop_table("tracks")
