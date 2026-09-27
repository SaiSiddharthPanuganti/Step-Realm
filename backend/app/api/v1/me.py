"""GET /me -- the current user.

The first endpoint behind :func:`get_current_user`, and therefore the proof
that the auth dependency actually gates the API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import UserResponse
from app.services import mana_service
from app.services.user_service import get_user_summary

router = APIRouter(tags=["me"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Current user: mana, college, owned tile count",
)
def read_me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> UserResponse:
    # Settle mana before reading it, so the balance reflects regen accrued
    # while the app was closed rather than only what the client last saw.
    # This is what makes "reconcile against the server after a gap" work: the
    # server's answer is always current, and the client never has to model
    # elapsed time itself.
    settled, _ = mana_service.refresh_mana(session, user)
    return get_user_summary(session, settled)
