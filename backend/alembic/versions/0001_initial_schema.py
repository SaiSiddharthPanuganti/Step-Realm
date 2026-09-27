"""initial schema: colleges, users, hex_tiles

Revision ID: 0001
Revises:
Create Date: 2026-09-27

Notes
-----
Creation order is forced by foreign keys: colleges -> users -> hex_tiles.

The world is an unbounded procedural hex plane projected over a real-world
map, so hex_tiles holds *claimed* tiles only. Rows are materialised by the
claim transaction, and uq_hex_tiles_q_r is the database-level guarantee that
one coordinate can never have two owners -- including when two requests race
before either has inserted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- colleges ----------------------------------------------------------
    op.create_table(
        "colleges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("join_code", sa.String(length=16), nullable=False),
        sa.Column("total_tile_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_colleges")),
        sa.UniqueConstraint("join_code", name="uq_colleges_join_code"),
    )
    op.create_index("ix_colleges_name", "colleges", ["name"], unique=False)

    # -- users -------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("mana", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_mana_regen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("daily_mana_earned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("daily_mana_reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("college_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["college_id"],
            ["colleges.id"],
            name=op.f("fk_users_college_id_colleges"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=False)
    op.create_index("ix_users_college_id", "users", ["college_id"], unique=False)
    # Case-insensitive uniqueness. A plain UNIQUE on email would allow
    # "A@b.com" and "a@b.com" to coexist and make login ambiguous.
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=True,
    )

    # -- hex_tiles ---------------------------------------------------------
    op.create_table(
        "hex_tiles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("q", sa.Integer(), nullable=False),
        sa.Column("r", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
        # defense_level is reserved: there is no PvP in this MVP, so nothing
        # writes a non-default value.
        sa.Column("defense_level", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_hex_tiles_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hex_tiles")),
        # The core invariant of the territory system.
        sa.UniqueConstraint("q", "r", name="uq_hex_tiles_q_r"),
    )
    op.create_index("ix_hex_tiles_owner_id", "hex_tiles", ["owner_id"], unique=False)


def downgrade() -> None:
    # Reverse dependency order.
    op.drop_index("ix_hex_tiles_owner_id", table_name="hex_tiles")
    op.drop_table("hex_tiles")

    op.drop_index("uq_users_email_lower", table_name="users", postgresql_where=None)
    op.drop_index("ix_users_college_id", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    op.drop_index("ix_colleges_name", table_name="colleges")
    op.drop_table("colleges")
