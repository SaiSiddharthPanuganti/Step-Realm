"""``users`` table.

The mobile client is untrusted on every write path, so every column here is
server-owned: the client sends *intents* (``/mana/sync`` step deltas,
``/territory/claim`` coordinates) and this table is the only place game state
is decided.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.core.constants import DISPLAY_NAME_MAX_LENGTH

if TYPE_CHECKING:
    from app.models.college import College
    from app.models.hex_tile import HexTile


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    """bcrypt digest only. Plaintext is never stored, logged, or returned by
    any schema in app/schemas."""

    display_name: Mapped[str] = mapped_column(
        String(DISPLAY_NAME_MAX_LENGTH), nullable=False
    )
    """The public name shown on the map and the leaderboard.

    NOT NULL and globally unique, because the world map has to attribute every
    claimed tile to *someone*: with no public name, the only identifier
    available was an email address, and publishing those to everyone who loads
    a map is not acceptable.

    Derived from the email's local part at signup (see
    :mod:`app.services.display_name`). Whether a player may change it later is
    an open product question -- a rename endpoint needs reserved words, an
    impersonation policy, and its own rate limit.

    Uniqueness is case-insensitive via ``uq_users_display_name_lower``, so
    "Alice" and "alice" cannot both exist and one cannot impersonate the
    other's territory label.
    """

    # -- mana --------------------------------------------------------------
    mana: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    last_mana_regen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """Watermark for lazy passive regeneration. Read on every user load,
    advanced to the last *whole* elapsed period, and deliberately not
    advanced past MANA_MAX so that offline time is discarded rather than
    banked (see app.core.constants.MANA_MAX)."""

    daily_mana_earned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Mana earned from walking since the last daily reset. Subject to
    DAILY_WALKING_MANA_CAP. Passive regen is tracked implicitly by
    last_mana_regen_at and is not capped by this."""

    daily_mana_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """Instant at which daily_mana_earned returns to zero. Advanced on every
    user load; if it is in the past the counters are zeroed first."""

    pending_step_remainder: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Steps walked (0..STEPS_PER_MANA_BLOCK-1) that have not yet made a
    whole mana block.

    Added in migration 0002 and not in the spec's original data model. Without
    it, a player who walks 1,500 steps would get 20 mana and silently lose the
    other 500, and would lose up to 19 mana on every single sync. Carrying the
    fraction forward is the only faithful reading of "1,000 steps = 20 mana"."""

    # -- step anti-cheat (phase 6) ------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    """Server-side account creation time.

    Added in migration 0005 purely to give the walking-speed cap a baseline for
    a player who has never synced steps. A client cannot influence it, which is
    the whole point -- see :data:`last_step_sync_at`."""

    last_step_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """When steps were last *credited* to this player, used by
    :func:`app.services.mana_service.check_step_plausibility`.

    NULL means "no steps have ever been credited", which is *not* an unlimited
    allowance -- that would hand every new account a free maximum-size sync and
    with it a full mana bar. NULL is measured from ``created_at`` instead.

    Deliberately advanced only when steps are actually credited. Two reasons, and
    the second is the important one:

    * A rejected sync must not move it, or a client could probe the allowance
      downward until it is zero and lock itself out, and a real player who
      tripped the cap once could never sync again.
    * A zero-step sync must not move it either. Foregrounding the app does not
      walk, and if a 0-step refresh reset the baseline then a player who opens
      the app every few minutes would find their next genuine sync measured
      against a two-second window and rejected.

    A consequence worth stating: a player can hold the baseline *older* than it
    needs to be by never syncing, and the allowance keeps growing. That is
    correct -- it is exactly the offline-walk case the allowance is sized for --
    and the daily mana cap is what bounds what the extra distance is worth.
    """

    # -- college (optional, phase 7) --------------------------------------
    college_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("colleges.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    """NULL for the ordinary player. The college group is an optional
    competitive layer, so the column is nullable and every college-derived
    read must tolerate NULL."""

    # -- relationships -----------------------------------------------------
    college: Mapped["College | None"] = relationship(
        "College", back_populates="members", lazy="joined"
    )

    tiles: Mapped[list["HexTile"]] = relationship(
        "HexTile", back_populates="owner", lazy="select"
    )

    __table_args__ = (
        # Case-insensitive uniqueness. The application lowercases and strips
        # email on write, and this index makes the guarantee hold in the
        # database too rather than trusting application code alone. A plain
        # UNIQUE on the column would permit "A@b.com" and "a@b.com" to
        # coexist and then make login ambiguous.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        # Same reasoning for display_name, and the stricter of the two: a
        # plain UNIQUE would let "Alice" and "alice" both exist, so a player
        # could label their territory as though it belonged to someone else.
        Index(
            "uq_users_display_name_lower",
            text("lower(display_name)"),
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id}>"
