"""add users.pending_step_remainder for fractional step carry-over

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27

Why this column exists
----------------------
The spec's walking reward is "1,000 steps = 20 mana" but the documented
``users`` table has nowhere to record a partial block. Settling whole blocks
only, and discarding the fraction, would silently destroy up to 19 mana per
sync for any player who does not walk in exact 1000-step multiples -- which
is almost everybody, since pedometer deltas are essentially never round.

``pending_step_remainder`` holds 0..999 steps that have been walked but not
yet converted into a whole block. It is a game-economy field, not analytics:
without it the reward rule cannot be implemented faithfully.

This is the one place the implementation departs from the data model in the
spec, and it is additive -- no existing column changes meaning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Adding a NOT NULL column with a constant default is a metadata-only
    # operation in PostgreSQL 11+, so this does not rewrite the table even
    # once users grows large.
    op.add_column(
        "users",
        sa.Column(
            "pending_step_remainder",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "pending_step_remainder")
