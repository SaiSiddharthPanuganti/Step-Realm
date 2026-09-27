"""display_name on users -- the public label for a player's territory.

Revision ID: 0003
Revises: 0002

Additive, and NOT NULL. Added in three steps rather than declared NOT NULL
straight away, because Postgres cannot add a NOT NULL column to a table that
already has rows without a default, and there is no sensible default for a
name that has to be globally unique.

The backfill is written to be correct on a non-empty table even though the
development database has no users yet. The subtle part is uniqueness: naive
per-row de-duplication can itself collide, because one user's *suffixed* name
can equal another user's *unsuffixed* stem ("alice" duplicated for id 5 becomes
"alice_5", which is also a legitimate stem in its own right). So the migration
either uses every stem as-is, or suffixes the whole table with the row id --
never a per-row mixture, which is the only version that can collide.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


DISPLAY_NAME_MAX_LENGTH = 24
MIN_LENGTH = 3

#: Everything outside this set becomes an underscore, matching
#: app.services.display_name.ALLOWED_CHARACTERS.
_SANITISE = r"[^A-Za-z0-9_]"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "display_name",
            sa.String(length=DISPLAY_NAME_MAX_LENGTH),
            nullable=True,
        ),
    )

    # 1. Sanitise the email's local part into a candidate stem, collapsing runs
    #    of underscores and trimming the ends, exactly as the application does.
    op.execute(
        sa.text(
            """
            UPDATE users
               SET display_name = left(
                       trim(both '_' from
                            regexp_replace(
                                regexp_replace(
                                    split_part(email, '@', 1),
                                    :bad, '_', 'g'),
                                '_+', '_', 'g')),
                       :max_len)
            """
        ).bindparams(bad=_SANITISE, max_len=DISPLAY_NAME_MAX_LENGTH)
    )

    # 2. Too-short results (e.g. "a@b.com") fall back to a fixed stem.
    op.execute(
        sa.text(
            "UPDATE users SET display_name = 'walker' "
            "WHERE display_name IS NULL OR length(display_name) < :min_len"
        ).bindparams(min_len=MIN_LENGTH)
    )

    # 3. Resolve collisions. All-or-nothing on purpose: mixing suffixed and
    #    unsuffixed rows is exactly what can produce a fresh collision, because
    #    one row's "alice_5" can be another's already-legal stem.
    #
    #    Decided in Python rather than in a DO $$ ... $$ block because bind
    #    parameters do not work inside PL/pgSQL: Postgres compiles the body as
    #    a function with no parameters and fails with "there is no parameter
    #    $1". A plain statement pair keeps the parameters working and reads
    #    more clearly.
    connection = op.get_bind()
    total = connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one()
    distinct = connection.execute(
        sa.text("SELECT count(DISTINCT lower(display_name)) FROM users")
    ).scalar_one()

    if distinct != total:
        op.execute(
            sa.text(
                """
                UPDATE users
                   SET display_name = left(
                           display_name,
                           :max_len - length('_' || id::text))
                           || '_' || id::text
                """
            ).bindparams(max_len=DISPLAY_NAME_MAX_LENGTH)
        )

    op.alter_column(
        "users", "display_name", existing_type=sa.String(DISPLAY_NAME_MAX_LENGTH),
        nullable=False,
    )
    op.create_index(
        "uq_users_display_name_lower",
        "users",
        [sa.text("lower(display_name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_users_display_name_lower", table_name="users")
    op.drop_column("users", "display_name")
