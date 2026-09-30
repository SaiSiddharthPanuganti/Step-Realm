"""walk_progress table and users.last_walk_report_at -- the claim-distance ledger.

Revision ID: 0006
Revises: 0005

Phase 8's claim rule is that a hex may only be claimed once the player has
covered ``min_claim_distance_meters`` (20 by default) *inside* it. That needs a
server-owned ledger of metres walked per (player, hex), because a claim gate
that believed the client's own word would be a gate drawn in pencil.

Two things land here:

* ``walk_progress`` -- one row per (user_id, q, r), accumulating metres across
  ``POST /territory/walk`` reports. Rows appear on first report and are deleted
  by a successful claim (progress is spent, not banked).
* ``users.last_walk_report_at`` -- nullable, NULL meaning "never reported". It
  is the baseline for the same replausibility check the step pipeline runs:
  a reported distance is judged against the time since the previous report and
  a plausible walking speed. NULL falls back to ``created_at`` (added in 0005),
  which is why this revision depends on it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "walk_progress",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("q", sa.Integer(), nullable=False),
        sa.Column("r", sa.Integer(), nullable=False),
        sa.Column("meters", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The unique (user_id, q, r) doubles as the per-player index that /world's
    # progress probe scans (filters on user_id first, then the q/r range).
    op.create_index(
        "uq_walk_progress_user_q_r", "walk_progress", ["user_id", "q", "r"], unique=True
    )

    op.add_column(
        "users",
        sa.Column(
            "last_walk_report_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "last_walk_report_at")
    op.drop_index("uq_walk_progress_user_q_r", table_name="walk_progress")
    op.drop_table("walk_progress")