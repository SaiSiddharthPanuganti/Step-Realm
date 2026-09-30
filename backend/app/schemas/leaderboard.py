"""Response schema for the leaderboard (phase 8)."""

from __future__ import annotations

from pydantic import BaseModel


class LeaderboardEntry(BaseModel):
    """One ranked player. Rank is competition-style: ties share it (1, 2, 2, 4)."""

    rank: int
    user_id: int
    display_name: str
    mana: int
    owned_tile_count: int


class LeaderboardViewer(BaseModel):
    """The caller's own standing, always included.

    ``rank`` is the player's global position, or None while they still own
    nothing ("unranked" rather than "last").
    """

    user_id: int
    display_name: str
    mana: int
    owned_tile_count: int
    rank: int | None


class LeaderboardResponse(BaseModel):
    limit: int
    degraded: bool
    entries: list[LeaderboardEntry]
    viewer: LeaderboardViewer | None