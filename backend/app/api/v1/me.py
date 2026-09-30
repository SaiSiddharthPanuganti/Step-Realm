"""GET /me and PATCH /me/display-name -- the current user.

The first endpoint behind :func:`get_current_user`, and therefore the proof
that the auth dependency actually gates the API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import DisplayNameUpdate, UserResponse
from app.services import mana_service
from app.services.user_service import (
    DisplayNameTaken,
    UserNotFound,
    get_user_summary,
    set_display_name,
)

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


@router.patch(
    "/me/display-name",
    response_model=UserResponse,
    summary="Choose or change the public display name",
)
def change_display_name(
    payload: DisplayNameUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_db),
) -> UserResponse:
    """Replace the email-derived name with the one the player chose.

    This is the endpoint the onboarding screen calls: a brand-new account has
    no name yet worth keeping, and the first thing the app asks is what the
    rest of the world should see. It also serves a later rename. The response
    is the refreshed profile, so the client can drop the new name straight
    into place without a GET.

    Two distinct failures: 409 when the name is someone else's (uniqueness is
    case-insensitive, so "maya" and "Maya" collide), and 422 when the
    character rules do not accept it -- caught by ``DisplayNameUpdate``
    before this handler runs.
    """
    try:
        updated = set_display_name(session, user.id, payload.display_name)
    except UserNotFound:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except DisplayNameTaken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That display name is already taken by another player",
        ) from None
    return get_user_summary(session, updated)
