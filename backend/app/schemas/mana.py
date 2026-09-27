"""Wire models for the mana system."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.constants import MAX_STEPS_PER_SYNC


class ManaSyncRequest(BaseModel):
    steps_delta: int = Field(ge=0, le=MAX_STEPS_PER_SYNC)
    """Steps walked *since the last successful sync*, not a running total.

    Named ``steps_delta`` rather than ``steps`` to make that explicit at the
    call site: a client that resends an absolute count would be credited for
    the same walk repeatedly.

    The bounds are input validation, not anti-cheat -- see
    :data:`app.core.constants.MAX_STEPS_PER_SYNC`. The real economic limit is
    the daily mana cap, which bounds what any step count can be worth.
    """


class ManaSyncResponse(BaseModel):
    """Complete server-side mana state after a sync.

    Returned in full so the client can replace its local display with server
    truth after an offline gap, as the spec requires, rather than trying to
    replay the elapsed time itself.
    """

    mana: int
    mana_max: int

    passive_mana_gained: int
    walking_mana_gained: int
    total_mana_gained: int

    steps_credited: int
    """Steps actually absorbed by this sync."""

    pending_step_remainder: int
    """Steps banked toward the next whole block. The client should carry this
    locally for display only and never trust its own copy after a gap."""

    daily_mana_earned: int
    daily_mana_remaining: int
    daily_reset_at: datetime

    mana_forfeited_to_daily_cap: int
    """Mana lost because today's walking cap was already reached. Non-zero
    here is the explanation a player needs for a disappointing walk."""

    mana_forfeited_to_mana_max: int
    """Mana earned but absorbed by a full bar."""
