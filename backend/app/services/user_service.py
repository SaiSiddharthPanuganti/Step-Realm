"""Read models about a user, and the display-name write path.

Kept separate from :mod:`app.services.auth_service` because those are
signup/login concerns while this assembles what a client displays and handles
a player renaming themselves.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import MANA_MAX
from app.models.hex_tile import HexTile
from app.models.user import User
from app.schemas.user import UserResponse
from app.services import auth_service, mana_service

#: The locked read raises the mana service's own exception; re-exporting it
#: here keeps the route handling one import.
UserNotFound = mana_service.UserNotFound


class DisplayNameTaken(Exception):
    """Another player already holds this display name (case-insensitive)."""


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


def set_display_name(session: Session, user_id: int, display_name: str) -> User:
    """Persist a player-chosen display name. Commits, or raises.

    Takes the same ``users`` row lock every write path takes, partly for the
    serializer's own sake and partly so a rename can never race a mana or walk
    write in this session's identity map. Uniqueness is the database's
    authority, exactly as it is for signup: the application does *not* trust a
    SELECT-then-INSERT guess, because two players renaming to "alice" at once
    would both pass the pre-check and then race the unique index. The
    ``IntegrityError`` is translated the same way ``auth_service.create_user``
    translates it, so no constraint name leaks into an error message.
    """
    user = mana_service.load_user_for_update(session, user_id)

    user.display_name = display_name
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if (
            auth_service._violated_constraint(exc)
            == auth_service.DISPLAY_NAME_CONSTRAINT
        ):
            raise DisplayNameTaken(display_name) from exc
        raise
    return user
