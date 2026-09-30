"""GET /leaderboard: the top players by tiles claimed.

Ranked by territory, not mana: at MANA_MAX and a 20-mana claim almost every
active player sits on a full bar, so mana would render a wall of ties while
territory gives a real ordering. The ranking is read through the leaderboard
service, which answers from the Redis sorted set and falls back to a Postgres
recompute when Redis is unavailable -- the board must render either way,
because Redis is a cache, not the source of truth.

The viewer's own row is always included, so a player who has not cracked the
top ``limit`` still sees where they stand. Their balance is settled the same
way ``GET /me`` settles it, so the board of your own page shows the number
you would see on the profile screen.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.leaderboard import LeaderboardEntry, LeaderboardResponse
from app.services import leaderboard_service, mana_service

router = APIRouter(prefix="/leaderboard", tags=["leaderboard"])


@router.get("", response_model=LeaderboardResponse, summary="Top players by tiles claimed")
def get_leaderboard(
    limit: int = Query(
        default=leaderboard_service.LEADERBOARD_DEFAULT_LIMIT,
        ge=1,
        le=leaderboard_service.LEADERBOARD_MAX_LIMIT,
    ),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> LeaderboardResponse:
    """Rank the players with the most claimed hexes. Requires a token."""
    settled, _outcome = mana_service.refresh_mana(session, user)
    entries, degraded = leaderboard_service.top_entries(session, limit)
    return LeaderboardResponse(
        limit=limit,
        degraded=degraded,
        entries=[
            LeaderboardEntry(
                rank=row.rank,
                user_id=row.user_id,
                display_name=row.display_name,
                mana=row.mana,
                owned_tile_count=row.owned_tile_count,
            )
            for row in entries
        ],
        viewer=leaderboard_service.viewer_entry(session, settled),
    )