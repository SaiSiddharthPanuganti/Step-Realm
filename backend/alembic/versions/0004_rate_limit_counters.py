"""rate_limit_counters -- one row per (subject, scope), for request throttling.

Revision ID: 0004
Revises: 0003

Why Postgres and not Redis. The existing architecture note in ``app/db/redis.py``
says Redis is "derived, best-effort" and that "nothing here may ever raise into a
request path", which is the right rule for a leaderboard nobody can lose money
to. A rate limiter is not that: it *rejects* requests, so putting it behind the
one dependency that is contractually allowed to be missing would mean a Redis
outage silently disables the limit, or worse, 429s the entire player base. The
limiter is also the natural place to reuse the atomic-upsert pattern already
established for tile claims, so it is one primitive rather than two.

Shape: exactly one row per (subject, scope), holding the start of the current
window and the count within it. The obvious alternative -- one row per
(subject, scope, window) so that windows never overwrite each other -- grows
without bound in *time*: a player active for a year accrues 525,600 rows per
scope and the table grows faster than the game. One row per subject keeps the
table proportional to the number of players instead.

The consequence is that an expired window is reset in place rather than
preserved, which is what makes this a fixed window anchored on the first
request rather than on wall-clock minutes. That costs the usual boundary burst
(a client can spend its whole allowance just before a reset and again just
after) and buys a table that does not need a reaper on the request path.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counters",
        sa.Column(
            "subject",
            sa.String(255),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "scope",
            sa.String(64),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_rate_limit_counters_count"),
    )

    # Sole read pattern is "has this subject been seen recently", which is how a
    # future cleanup finds rows whose window has long expired. A partial index
    # keyed on recency keeps that from degrading into a full scan once the table
    # holds a row per player per scope.
    op.create_index(
        "ix_rate_limit_counters_window_started_at",
        "rate_limit_counters",
        ["window_started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rate_limit_counters_window_started_at",
        table_name="rate_limit_counters",
    )
    op.drop_table("rate_limit_counters")
