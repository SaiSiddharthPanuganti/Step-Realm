"""Password hashing and JWT issuance/verification.

Two rules this module exists to enforce:

1. Plaintext passwords never leave here. Nothing logs them, and no schema in
   ``app/schemas`` exposes ``password_hash``.
2. Token verification is an explicit allowlist of algorithms. Accepting the
   algorithm named in the token's own header is how ``alg: none`` and HS/RS
   confusion attacks get in.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, TypedDict

import bcrypt
import jwt

from app.core.config import get_settings

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TYPE: Literal["access"] = "access"


class TokenClaims(TypedDict):
    """Exactly the claims the spec requires: sub, type, iat, nbf, exp."""

    sub: str
    """User id as a *string*. RFC 7519 defines ``sub`` as a StringOrURI, and
    PyJWT rejects a non-string subject."""

    type: str
    """Discriminates token kinds so a future refresh token cannot be replayed
    as an access token."""

    iat: int
    nbf: int
    exp: int
    """NumericDate (seconds since the epoch), per RFC 7519."""


class TokenError(Exception):
    """Raised for any token that fails verification.

    The message is deliberately coarse. A caller turning this into an
    HTTPException must not leak *why* a token was rejected.
    """


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return a bcrypt digest. Never reversible."""
    rounds = get_settings().bcrypt_rounds
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a password against a stored digest."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        # A malformed or truncated digest in the database. Treat it as a
        # failed login rather than letting a 500 leak the shape of the row.
        logger.warning("stored password hash is malformed; denying login")
        return False


_dummy_hash: str | None = None


def _get_dummy_hash() -> str:
    """Lazily built digest of a value nobody can supply.

    Login does this when the email is unknown, so that "no such user" and
    "wrong password" take the same time. Without it, response latency is a
    free account-enumeration oracle: an attacker can discover which emails
    are registered without ever guessing a password.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("steprealm-timing-equaliser")
    return _dummy_hash


def verify_password_or_dummy(password: str, password_hash: str | None) -> bool:
    """Verify against ``password_hash``, or burn equivalent CPU when it is
    ``None``. Returns False in the ``None`` case."""
    if password_hash is None:
        bcrypt.checkpw(password.encode("utf-8"), _get_dummy_hash().encode("utf-8"))
        return False
    return verify_password(password, password_hash)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def create_access_token(
    user_id: int, *, token_type: Literal["access"] = ACCESS_TOKEN_TYPE
) -> tuple[str, datetime]:
    """Issue an access token. Returns ``(token, expires_at)``.

    ``nbf`` equals ``iat`` rather than being omitted: the spec asks for it
    explicitly, and setting it to the issue time means the token is valid the
    instant it is minted with no clock-skew window to tune.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    issued = int(now.timestamp())

    claims: TokenClaims = {
        "sub": str(user_id),
        "type": token_type,
        "iat": issued,
        "nbf": issued,
        "exp": int(expires_at.timestamp()),
    }

    token = jwt.encode(
        claims,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, expires_at


def decode_access_token(token: str) -> int:
    """Verify a bearer token and return the user id it names.

    Raises :class:`TokenError` for anything invalid. Callers should not
    distinguish the reasons when surfacing an error to a client.
    """
    settings = get_settings()

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Allowlist, not the token's own header. Trusting the header is
            # what permits alg=none and HS/RS key-confusion attacks.
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "nbf", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token invalid") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenError("unexpected token type")

    subject = payload.get("sub")
    try:
        return int(subject)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TokenError("malformed subject") from exc
