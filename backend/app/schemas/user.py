"""Wire models for the current user and their college."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import (
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
)
from app.services.display_name import ALLOWED_CHARACTERS


class CollegeSummary(BaseModel):
    """Read-only view of a college.

    ``join_code`` is included because the owning member needs to see it in
    order to share it. Never expose a college's member list this way.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    join_code: str


class UserResponse(BaseModel):
    """The authenticated user, as returned by GET /me and the auth endpoints.

    Note what is absent: ``password_hash``, ``last_mana_regen_at`` and
    ``daily_mana_earned`` are server bookkeeping and are deliberately not on
    the wire. ``from_attributes`` reads them off the ORM object, so adding a
    field here is the only way to expose one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    """The public name. Safe to show to other players, unlike ``email``.

    Returned here because ``/me`` is the authenticated player's *own* view, so
    the email is fine to include. The world endpoint deliberately omits it.
    """
    mana: int
    mana_max: int
    """Served rather than hardcoded in the client, so the mana bar on the
    profile screen cannot drift from the server's cap."""
    college: CollegeSummary | None
    owned_tile_count: int


class DisplayNameUpdate(BaseModel):
    """A player-chosen replacement for the email-derived name.

    Character rules mirror :mod:`app.services.display_name` exactly -- the
    same characters the signup derivation produces, so a name the player types
    could just as well have been generated -- but unlike derivation this is
    *strict*: a chosen name with an emoji or a space is rejected up front (422)
    rather than silently rewritten, because the player asked for a specific
    name and "we changed it" is worse than "try again".
    """

    display_name: str = Field(
        min_length=DISPLAY_NAME_MIN_LENGTH,
        max_length=DISPLAY_NAME_MAX_LENGTH,
    )

    @field_validator("display_name")
    @classmethod
    def _valid_display_name(cls, value: str) -> str:
        stripped = value.strip()
        invalid = sorted({ch for ch in stripped if ch not in ALLOWED_CHARACTERS})
        if invalid:
            raise ValueError(
                "Display names may only use letters, digits and underscores; "
                f"found: {', '.join(invalid)}"
            )
        return stripped
