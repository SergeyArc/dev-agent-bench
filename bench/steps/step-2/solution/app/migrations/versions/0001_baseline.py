"""Baseline: an empty schema that later revisions build on.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create nothing: the service has no tables yet."""


def downgrade() -> None:
    """Drop nothing."""
