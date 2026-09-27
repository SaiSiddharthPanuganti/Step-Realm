"""``hex_tiles`` table -- the territory layer.

The world is an *unbounded procedural* hex plane projected over a real-world
map, not a pre-seeded arena, so rows are materialised lazily: a tile row
exists if and only if some player has claimed it. The client renders unowned
hexes itself from the viewport bounds returned by ``GET /world``.

That makes the ``(q, r)`` unique constraint below the only thing standing
between two concurrent claim requests and a duplicated tile, which is why it
is a database constraint rather than application logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class HexTile(Base):
    __tablename__ = "hex_tiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    q: Mapped[int] = mapped_column(Integer, nullable=False)
    r: Mapped[int] = mapped_column(Integer, nullable=False)
    """Axial coordinates. Not offset, not cube -- axial on the wire and in
    storage, with cube used only transiently inside the rounding routine in
    app/services/geo.py."""

    owner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    """NULL only transiently. Because rows are created at claim time, a
    committed row is always owned -- but the column stays nullable because
    the claim path needs to insert-then-lock, and because defense_level
    tooling in a future phase may want unowned rows."""

    defense_level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Reserved. There is no PvP or attacking in this MVP, so nothing writes
    a non-default value; the column exists because the spec's data model
    includes it and adding it later is a needless migration."""

    owner: Mapped["User | None"] = relationship("User", back_populates="tiles")

    __table_args__ = (
        # The core invariant of the whole territory system: one row per
        # (q, r). Enforced here so that even a bug in application code, or
        # two requests racing before either has inserted, cannot produce two
        # owners for the same tile.
        UniqueConstraint("q", "r", name="uq_hex_tiles_q_r"),
        # Backs the per-user tile count in GET /me and the leaderboard.
        Index("ix_hex_tiles_owner_id", "owner_id"),
    )

    def __repr__(self) -> str:
        return f"<HexTile q={self.q} r={self.r} owner_id={self.owner_id}>"
