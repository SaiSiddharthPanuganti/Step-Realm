"""``colleges`` table.

An optional competitive layer: students join by code to pool tile counts and
compete on the college leaderboard. Ordinary players never set college_id and
this table is simply unused for them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class College(Base):
    __tablename__ = "colleges"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)

    join_code: Mapped[str] = mapped_column(String(16), nullable=False)
    """Opaque code players type to join. Compared case-insensitively by
    normalising to upper case on write; the spec asks for a simple unique
    match and this stays that simple."""

    total_tile_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    """Denormalised sum of the tiles owned by this college's members.

    Incremented inside the same transaction that assigns a tile's owner (see
    territory_service.claim_tile) so the two can never disagree after a
    commit. Redis mirrors this for the leaderboard, but Redis is never
    authoritative -- Postgres is.

    FLAG: this is a counter, not a derived total, so it can only ever be
    trusted if nothing else mutates hex_tiles.owner_id. There is no user
    deletion in the MVP, but when that arrives, the cascade on
    hex_tiles.owner_id will silently desynchronise this column. Add a
    reconciliation query before shipping account deletion."""

    members: Mapped[list["User"]] = relationship(
        "User", back_populates="college", lazy="select"
    )

    __table_args__ = (
        # Joins are always by code, so this is the hot path. The unique
        # constraint's btree serves the lookup as well as the guarantee, so
        # no separate index is warranted.
        UniqueConstraint("join_code", name="uq_colleges_join_code"),
    )

    def __repr__(self) -> str:
        return f"<College id={self.id} name={self.name!r}>"
