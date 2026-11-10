"""Tracks and their segments.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tracks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name="tracks_pkey"),
        sa.UniqueConstraint("code", name="uq_tracks_code"),
    )
    op.create_table(
        "track_segments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="track_segments_pkey"),
        sa.ForeignKeyConstraint(
            ["track_id"], ["tracks.id"], name="fk_track_segments_track_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("track_id", "label", name="uq_track_segments_track_id_label"),
    )


def downgrade() -> None:
    op.drop_table("track_segments")
    op.drop_table("tracks")
