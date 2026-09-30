"""``walk_progress`` table -- how far a player has walked inside a hex.

The "walk 20 m inside a hex before you can claim it" rule needs a ledger the
server itself wrote. Clients report metres walked on ``POST /territory/walk``;
this table accumulates them per ``(user_id, q, r)``, and the claim path reads
the total. Nothing else -- in particular, not the client -- is trusted to say
how far a player has come.

Rows materialise lazily on the first report, the same pattern as ``hex_tiles``:
there is no row for a hex nobody has walked in, and a player who has never
touched a hex has zero metres there by definition. A row is deleted when the
tile is successfully claimed (``territory_service.claim_tile``), so progress is
*spent* by a claim, not banked.

Keeping this separate from ``hex_tiles`` is a rule rather than tidiness. The
ownership row answers "who holds this tile" and is hot under claims; the
progress ledger answers "how far has *this player* come here", is written by a
different endpoint, and must be deletable without touching the ownership row's
lock ordering.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class WalkProgress(Base):
    __tablename__ = "walk_progress"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    q: Mapped[int] = mapped_column(Integer, nullable=False)
    r: Mapped[int] = mapped_column(Integer, nullable=False)
    """Axial coordinates, identical in meaning to ``hex_tiles.q/r`` -- the
    same ``app.services.geo`` projection produces both, so the two tables can
    never disagree about which hex a coordinate is."""

    meters: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    """Metres walked inside this hex by this player, accumulated from reports.

    Intent: a *delta* accumulated in place, not a monotonically growing event
    log. The client submits metres since its previous report, the row keeps the
    running total, and a claim reads the total it needs. There is no event log
    to archive: the game only ever asks "have you walked far enough here"."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    user: Mapped["User"] = relationship("User", back_populates="walk_progress")

    __table_args__ = (
        # One ledger row per (player, hex). The unique constraint doubles as the
        # per-player lookup index for /world's progress probe, which filters on
        # user_id first and then the q/r range.
        Index("uq_walk_progress_user_q_r", "user_id", "q", "r", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<WalkProgress user_id={self.user_id} q={self.q} r={self.r} "
            f"meters={self.meters}>"
        )