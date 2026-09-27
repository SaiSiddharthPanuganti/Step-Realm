"""created_at and last_step_sync_at on users -- the baseline for the speed cap.

Revision ID: 0005
Revises: 0004

Phase 6's step anti-cheat rejects a step delta that no real walking could have
produced in the time since the player's previous sync. That comparison needs a
server-side timestamp of the previous sync, which nothing recorded before --
``last_mana_regen_at`` is deliberately *not* it, because regeneration advances on
every read of a user row including ``GET /me`` and every claim, so it tracks
login activity rather than walking.

``last_step_sync_at`` is nullable and NULL means "this player has never had
steps credited". A NULL is not an unlimited allowance -- that would hand every
fresh account a free 250,000 steps, enough to max the mana bar on signup -- so it
falls back to ``created_at``. Which is why that column is added here rather than
assumed: without a server-side record of when the account appeared there is
nothing to measure a first sync against.

Both are ``server_default now()`` / nullable so the migration is instant on a
populated table. PG fills existing rows with the migration's transaction time,
which is a lie of a few seconds' magnitude for the accounts that exist at
upgrade time and is not worth a table rewrite to avoid.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "last_step_sync_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "last_step_sync_at")
    op.drop_column("users", "created_at")
