"""Read models about a user.

Kept separate from :mod:`app.services.auth_service` because those are
write-path concerns (creating accounts, checking credentials) while this
assembles what a client displays.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constants import MANA_MAX
from app.models.hex_tile import HexTile
from app.models.user import User
from app.schemas.user import UserResponse


def count_owned_tiles(session: Session, user_id: int) -> int:
    """How many tiles this player owns.

    Backed by ``ix_hex_tiles_owner_id``. At the MVP's scale a COUNT is
    cheaper and far simpler than maintaining a denormalised counter that
    would need reconciling whenever ownership changes.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(HexTile)
            .where(HexTile.owner_id == user_id)
        )
        or 0
    )


def get_user_summary(session: Session, user: User) -> UserResponse:
    """Assemble the GET /me payload.

    ``user.mana`` is the stored value. Callers must settle regeneration first
    (:func:`app.services.mana_service.refresh_mana`), so that by the time a
    client reads the user it sees mana that already includes regen accrued
    while it was closed.
    """
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        mana=user.mana,
        mana_max=MANA_MAX,
        college=user.college,
        owned_tile_count=count_owned_tiles(session, user.id),
    )
