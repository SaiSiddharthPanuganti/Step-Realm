"""POST /mana/sync -- submit a step delta and receive authoritative mana state."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.constants import DAILY_WALKING_MANA_CAP, MANA_MAX
from app.db.session import get_db
from app.models.user import User
from app.schemas.mana import ManaSyncRequest, ManaSyncResponse
from app.services import mana_service
from app.services.mana_service import ManaOutcome, UserNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mana", tags=["mana"])


def build_sync_response(user: User, outcome: ManaOutcome) -> ManaSyncResponse:
    return ManaSyncResponse(
        mana=user.mana,
        mana_max=MANA_MAX,
        passive_mana_gained=outcome.passive_gained,
        walking_mana_gained=outcome.walking_gained,
        total_mana_gained=outcome.total_gained,
        steps_credited=outcome.steps_credited,
        pending_step_remainder=user.pending_step_remainder,
        daily_mana_earned=user.daily_mana_earned,
        daily_mana_remaining=max(
            0, DAILY_WALKING_MANA_CAP - user.daily_mana_earned
        ),
        daily_reset_at=user.daily_mana_reset_at,
        mana_forfeited_to_daily_cap=outcome.mana_forfeited_to_daily_cap,
        mana_forfeited_to_mana_max=outcome.mana_forfeited_to_mana_max,
    )


@router.post(
    "/sync",
    response_model=ManaSyncResponse,
    summary="Submit a step delta, receive authoritative mana state",
)
def sync_mana(
    payload: ManaSyncRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> ManaSyncResponse:
    """Credit steps and settle regeneration in one locked transaction.

    The lock is the point of this endpoint. Without it, two syncs arriving
    together would read the same ``pending_step_remainder`` and
    ``daily_mana_earned``, and both would grant mana for the same steps.
    """
    try:
        updated, outcome = mana_service.sync_mana(
            session, user.id, payload.steps_delta
        )
    except mana_service.ImplausibleStepCount as exc:
        # 422, and the message carries the ceiling. The request was well formed
        # and the player is allowed to sync; this particular delta is not
        # physically possible given their last sync, which is a property of the
        # payload rather than a conflict with existing state. Saying "you have
        # synced too often" would be wrong -- they may well have walked all
        # afternoon, and the service simply has not seen it yet.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from None
    except UserNotFound:
        # Authenticated a moment ago, gone now. Same generic 401 as any other
        # invalid credential rather than a 404 that confirms the id existed.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    if outcome.mana_forfeited_to_daily_cap:
        logger.info(
            "walking reward clipped by daily cap",
            extra={
                "user_id": updated.id,
                "forfeited": outcome.mana_forfeited_to_daily_cap,
            },
        )

    return build_sync_response(updated, outcome)
