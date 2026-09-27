"""POST /auth/signup and POST /auth/login.

Thin by design: authenticate, delegate to the service, translate domain
errors to HTTP. No game logic lives in a route handler.

Both endpoints are rate limited per client address. They are the only
unauthenticated writes in the game and the only ones an attacker can hammer
without holding a token, and the limit is per address rather than per submitted
email on purpose -- see :data:`app.core.constants.AUTH_LOGIN_RATE_LIMIT_PER_MINUTE`
for why the obvious alternative is a denial-of-service tool.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import limit_per_ip
from app.core.config import get_settings
from app.core.constants import RATE_LIMIT_WINDOW_SECONDS
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, SignupRequest, TokenResponse
from app.services import rate_limit_service
from app.services.auth_service import (
    DisplayNameUnavailable,
    EmailAlreadyRegistered,
    InvalidCredentials,
    authenticate,
    create_user,
)
from app.services.user_service import get_user_summary

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _signup_limit():
    return limit_per_ip(
        rate_limit_service.SCOPE_AUTH_SIGNUP,
        lambda: get_settings().auth_signup_rate_limit_per_minute,
        RATE_LIMIT_WINDOW_SECONDS,
    )


def _login_limit():
    return limit_per_ip(
        rate_limit_service.SCOPE_AUTH_LOGIN,
        lambda: get_settings().auth_login_rate_limit_per_minute,
        RATE_LIMIT_WINDOW_SECONDS,
    )


def _issue_token(session: Session, user: User) -> TokenResponse:
    """Mint an access token and pair it with the user summary.

    Both signup and login return a usable token, so a new account does not
    have to immediately round-trip through /auth/login.
    """
    token, expires_at = create_access_token(user.id)
    from app.services.mana_service import utcnow

    return TokenResponse(
        access_token=token,
        expires_in_seconds=int((expires_at - utcnow()).total_seconds()),
        user=get_user_summary(session, user),
    )


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and receive a token",
)
def signup(
    payload: SignupRequest,
    session: Session = Depends(get_db),
    _limit: None = Depends(_signup_limit()),
) -> TokenResponse:
    try:
        user = create_user(session, email=payload.email, password=payload.password)
    except DisplayNameUnavailable as exc:
        # 503, not 409/400. The request was well-formed; the server could not
        # find a free name, which is transient and worth a retry. Crucially it
        # is *not* a 409: reporting "that name is taken" would tell the caller
        # another player already holds it, turning a cosmetic failure into a
        # probe for other players' names.
        logger.warning("signup: exhausted display name candidates")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not allocate a display name, please retry",
        ) from exc
    except EmailAlreadyRegistered:
        # 409 rather than 400: the request was well-formed, it conflicts with
        # existing state. Also the only place a client learns an address is
        # taken -- signup is where that is unavoidable, and login still
        # refuses to confirm it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        ) from None
    return _issue_token(session, user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a token",
)
def login(
    payload: LoginRequest,
    session: Session = Depends(get_db),
    _limit: None = Depends(_login_limit()),
) -> TokenResponse:
    try:
        user = authenticate(session, email=payload.email, password=payload.password)
    except InvalidCredentials:
        # One response for "no such user" and "wrong password", so login
        # cannot be used to enumerate registered addresses.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return _issue_token(session, user)
