"""Shared FastAPI dependencies.

The important one is :func:`get_current_user`, which is the single gate
between an untrusted client and every write path in the game. It runs before
any handler logic, so no route can accidentally perform game-affecting work
against an unauthenticated caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import TokenError, decode_access_token
from app.db.session import get_db, get_session_factory
from app.models.user import User
from app.services import rate_limit_service

logger = logging.getLogger(__name__)

#: Must match the login route exactly, or the generated OpenAPI "Authorize"
#: button sends the wrong grant. Paths are mounted unversioned to match the
#: API surface in the spec (POST /auth/login, GET /me, ...).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=True)

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": "Bearer"}

#: Same scheme, but ``auto_error=False`` so a missing token is not a 401. Used
#: only by endpoints that are meaningfully useful logged out.
optional_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login", auto_error=False
)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: Session = Depends(get_db),
) -> User:
    """Resolve the bearer token to a live user.

    Three separate rejections, one indistinguishable response: no token
    (handled by ``oauth2_scheme``, which 401s with the Bearer challenge),
    a bad token, and a valid token naming a user that no longer exists.
    """
    try:
        user_id = decode_access_token(token)
    except TokenError:
        # Message is intentionally generic. Distinguishing "expired" from
        # "malformed" tells an attacker which tokens to keep.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=_UNAUTHENTICATED_HEADERS,
        ) from None

    user = session.get(User, user_id)
    if user is None:
        logger.info("token for missing user %s rejected", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers=_UNAUTHENTICATED_HEADERS,
        )

    # NOTE for phase 5: this object is in the session's identity map, and it
    # was read *without* a row lock. The claim transaction must issue its own
    # ``SELECT ... FOR UPDATE`` on the user row rather than trusting this
    # instance, or two concurrent claims would both validate against the same
    # unlocked mana balance.
    return user


def get_optional_current_user(
    token: str | None = Depends(optional_oauth2_scheme),
    session: Session = Depends(get_db),
) -> User | None:
    """Resolve a token if one was sent, and treat its absence as anonymous.

    A *malformed or expired* token is also treated as anonymous rather than
    rejected. That is the right trade for ``GET /world``: the map is worth
    rendering before a player has logged in, and a hard 401 there would mean a
    stale token blocks the entire app shell from drawing. A client with a
    genuinely expired session still finds out immediately on ``GET /me``,
    which does 401, so this cannot hide the condition from a real client.
    """
    if token is None:
        return None

    try:
        user_id = decode_access_token(token)
    except TokenError:
        logger.debug("optional auth: bad token treated as anonymous")
        return None

    user = session.get(User, user_id)
    if user is None:
        logger.debug("optional auth: token for missing user %s", user_id)
    return user


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

#: A rate-limit ceiling, read at request time. A callable rather than an int so
#: that configuration is honoured per request and stays overridable in tests.
LimitSource = Callable[[], int]


def rate_limit_session() -> Iterator[Session]:
    """A database session owned by the limiter, not by the request handler.

    Deliberately not ``get_db``. ``rate_limit_service.enforce`` rolls back the
    session it is given when the counter upsert fails, because a Postgres
    transaction that has raised is aborted and unusable until then. Sharing the
    handler's session would make that rollback throw away the handler's
    in-flight work -- so that a limiter outage could destroy a claim. The extra
    connection is only taken by the handful of routes that opt in.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def client_address(request: Request) -> str:
    """The address to count a request against.

    ``request.client.host`` is the peer address, which behind a reverse proxy or
    a load balancer is the *proxy's* address, making a per-address limit a
    shared pool for every player on the platform. ``X-Forwarded-For`` fixes that
    and introduces a worse problem: it is a plain request header, so anyone can
    set it. Trusting it unconditionally turns the limiter into a tool for
    targeting a chosen victim -- forge a header naming them, burn their limit.

    Hence the opt-in ``trust_forwarded_for`` setting. It is safe only when the
    service genuinely cannot be reached except through a proxy that overwrites
    the header, and the leftmost entry is the original client in that setup
    (each hop appends, so the first is the one the edge saw). When the setting is
    off -- the default, and the right answer for local development and for a
    directly exposed container -- the peer address is used and the limit is
    merely blunt rather than forgeable.
    """
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Leftmost first: proxies append, so the first entry is the client
            # the edge originally saw. Falls back to the peer address for a
            # malformed header rather than trusting a partial parse.
            return forwarded.split(",")[0].strip() or (request.client.host or "unknown")

    return request.client.host or "unknown"


def _throttle(
    session: Session,
    *,
    subject: str,
    scope: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Count one request and refuse it with a 429 if the window is exhausted."""
    decision = rate_limit_service.enforce(
        session,
        subject=subject,
        scope=scope,
        limit=limit,
        window_seconds=window_seconds,
    )
    if not decision.allowed:
        logger.info(
            "rate limit exceeded",
            extra={"scope": scope, "subject": subject, "limit": limit},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Rate limit exceeded: {limit} requests per "
                f"{window_seconds}s. Try again shortly."
            ),
            headers={
                "Retry-After": str(decision.retry_after_seconds),
                # Lets a client see the remaining budget without spending a
                # request discovering it, and without a 429 being the only
                # signal that a limit exists at all.
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": str(decision.remaining),
            },
        )


def limit_per_user(scope: str, limit: LimitSource, window_seconds: int):
    """Build a dependency that throttles one authenticated player.

    Declares ``get_current_user`` so an unauthenticated caller is rejected with
    a 401 before any counter is touched. The guarantee comes from how FastAPI
    resolves a dependency: every sub-dependency is solved *before* this
    function's body runs, so a ``get_current_user`` that raises 401 means the
    body never executes. Swapping the two parameter order does not change that
    -- what matters is that the authentication failure happens during resolution
    rather than inside the body. Getting it wrong would let anyone who knew a
    player's token burn their allowance, and would report a 429 to a client
    whose real problem is a stale token.

    ``limit`` is a callable rather than a value so the ceiling is read at request
    time. Reading it once at import time would make the setting untestable --
    the app is built before any test can change configuration -- and would mean
    changing the limit needs a redeploy of every worker rather than a restart.
    """

    def dependency(
        user: User = Depends(get_current_user),
        session: Session = Depends(rate_limit_session),
    ) -> None:
        _throttle(
            session,
            subject=f"user:{user.id}",
            scope=scope,
            limit=limit(),
            window_seconds=window_seconds,
        )

    return dependency


def limit_per_ip(scope: str, limit: LimitSource, window_seconds: int):
    """Build a dependency that throttles unauthenticated traffic by address.

    For the endpoints reachable without a token, where there is no user to count
    against. See :func:`client_address` for the trust boundary on the address
    itself.
    """

    def dependency(
        request: Request,
        session: Session = Depends(rate_limit_session),
    ) -> None:
        _throttle(
            session,
            subject=f"ip:{client_address(request)}",
            scope=scope,
            limit=limit(),
            window_seconds=window_seconds,
        )

    return dependency
