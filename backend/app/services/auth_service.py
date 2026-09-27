"""Signup, login, and the account-existence rules.

This module owns the transaction boundary for account creation. It does not
trust the uniqueness check to application code: the *database* constraint
``uq_users_email_lower`` is the authority, and the application simply
translates its violation into a domain error. That is what makes concurrent
signup safe rather than merely usually-correct (see
tests/test_auth_concurrency.py).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.constants import DISPLAY_NAME_MAX_ATTEMPTS, INITIAL_MANA
from app.core.security import hash_password, verify_password_or_dummy
from app.models.user import User
from app.services import display_name
from app.services.mana_service import next_daily_reset, utcnow

logger = logging.getLogger(__name__)


class EmailAlreadyRegistered(Exception):
    """Raised when a signup collides with an existing account."""


class InvalidCredentials(Exception):
    """Raised on failed login.

    A single exception for "no such user" and "wrong password" on purpose --
    the API layer maps both to one 401, and any caller branching on them
    would reintroduce account enumeration.
    """


class DisplayNameUnavailable(Exception):
    """Raised when no display name could be found within the retry budget.

    Kept distinct from :class:`EmailAlreadyRegistered` on purpose. A display
    name collision is *not* the caller's fault and must not be reported as
    "that email is taken", which would be both untrue and a small oracle about
    other players' names. Mapping this to a 503 tells the client to retry
    without telling it anything about the world.
    """


DISPLAY_NAME_CONSTRAINT = "uq_users_display_name_lower"


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Which unique index did Postgres reject?

    Reads the constraint name out of the driver's message. Checking first and
    looping on one specific name keeps the retry from masking an unrelated
    failure as a display-name collision, and keeps the email path behaving
    exactly as it did before display names existed.
    """
    message = str(getattr(exc, "orig", exc))
    for name in (DISPLAY_NAME_CONSTRAINT, "uq_users_email_lower"):
        if name in message:
            return name
    return None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(session: Session, email: str) -> User | None:
    """Look a user up case-insensitively.

    Matches on ``lower(email)`` so it uses ``uq_users_email_lower`` and stays
    correct even if a row somehow holds mixed case.
    """
    return session.scalar(
        select(User).where(func.lower(User.email) == normalise_email(email))
    )


def create_user(session: Session, *, email: str, password: str) -> User:
    """Create an account, or raise :class:`EmailAlreadyRegistered`.

    Initial mana is :data:`INITIAL_MANA` and the regen watermark starts at
    signup time, so a new player's first tile is affordable immediately.

    A display name is derived from the email and retried with a random suffix
    if it is already taken. The uniqueness check is a *pre-check plus a
    database authority*, not a pre-check on its own: two simultaneous signups
    for ``alice@x.com`` and ``alice@y.com`` would both pass a SELECT and then
    race to insert, so the retry is driven by the unique index rejecting the
    commit rather than by a guess that the name was free.
    """
    now = utcnow()
    normalised = normalise_email(email)

    # Hashed once, outside the loop: bcrypt costs ~250ms and there is no reason
    # to pay it again for a name collision. The salt is embedded in the digest,
    # so one hash is as good as a fresh one per attempt.
    password_hash = hash_password(password)
    stem = display_name.stem_from_email(normalised)

    for attempt in range(DISPLAY_NAME_MAX_ATTEMPTS):
        candidate = display_name.candidate_for_attempt(stem, attempt)
        user = User(
            email=normalised,
            password_hash=password_hash,
            display_name=candidate,
            mana=INITIAL_MANA,
            last_mana_regen_at=now,
            daily_mana_earned=0,
            daily_mana_reset_at=next_daily_reset(now),
        )

        session.add(user)
        try:
            session.commit()
        except IntegrityError as exc:
            # Roll back before anything else: the session is in a failed state
            # and any further use raises PendingRollbackError.
            session.rollback()
            if _violated_constraint(exc) == DISPLAY_NAME_CONSTRAINT:
                # Someone holds that name. Try the next candidate. Note the
                # *object* is discarded rather than reused: after a rollback the
                # instance is transient again, and reusing it would re-add the
                # same rejected display_name.
                logger.info("signup: display name already taken, retrying")
                continue
            # Re-raise as a domain error without the original message, which
            # would contain the email and the constraint name.
            logger.info("signup rejected: email already registered")
            raise EmailAlreadyRegistered from exc

        return user

    raise DisplayNameUnavailable


def authenticate(session: Session, *, email: str, password: str) -> User:
    """Return the authenticated user, or raise :class:`InvalidCredentials`."""
    user = get_user_by_email(session, email)

    # Always spend the bcrypt time, even for an unknown email, so that
    # response latency does not disclose which addresses are registered.
    password_ok = verify_password_or_dummy(
        password, user.password_hash if user is not None else None
    )

    if user is None or not password_ok:
        raise InvalidCredentials

    return user
