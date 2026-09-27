"""Generating a player's public display name.

Why a display name at all: the world map and the phase-8 leaderboard both have
to say *who* holds a territory, and the only identifier ``users`` had was an
email address. Publishing an email to everyone who loads a map is not an
option, so a public name had to exist before the map could be drawn at all.

The name is derived from the email's local part rather than chosen by the
player, because a rename endpoint is a separate feature with its own problems
(reserved words, impersonation, rate limiting, a uniqueness race). This module
does the mechanical half of that: produce something safe, stable, and
readable. Whether players may later change it is a product decision, not a
technical one.

Everything here is a pure function of its input plus a random suffix, so the
rules are testable without a database. Uniqueness itself is *not* enforced
here -- that is the unique index's job, and the retry loop lives in
:mod:`app.services.auth_service` where the transaction is.
"""

from __future__ import annotations

import re
import secrets
import string

from app.core.constants import (
    DISPLAY_NAME_MAX_ATTEMPTS,
    DISPLAY_NAME_MAX_LENGTH,
    DISPLAY_NAME_MIN_LENGTH,
)

#: The only characters a display name may contain. Underscore instead of a
#: space, so a name is safe to render in a map label, a URL, and a terminal
#: without escaping.
ALLOWED_CHARACTERS = frozenset(string.ascii_letters + string.digits + "_")

#: Used when the email yields nothing usable, e.g. "a@b.com" or "___@x.com".
FALLBACK_STEM = "walker"

SUFFIX_LENGTH = 4
_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
"""36 characters, so 36**4 = ~1.7M distinct suffixes. Lowercase-only because
the stem keeps the email's own casing and a mixed-case suffix reads as noise."""

_COLLAPSE_UNDERSCORES = re.compile(r"_+")
_TRAILING_UNDERSCORES = re.compile(r"_+$")


def sanitise(stem: str) -> str:
    """Reduce arbitrary text to a legal display-name stem.

    Non-allowed characters become underscores, runs collapse, leading and
    trailing underscores are trimmed, and the result is truncated. Deliberately
    lossy and deliberately not reversible: this is a cosmetic label, not an
    identity claim, and anything cleverer invites a homograph problem.
    """
    replaced = "".join(
        character if character in ALLOWED_CHARACTERS else "_" for character in stem
    )
    collapsed = _COLLAPSE_UNDERSCORES.sub("_", replaced).strip("_")
    truncated = collapsed[:DISPLAY_NAME_MAX_LENGTH]
    return truncated.strip("_")


def stem_from_email(email: str) -> str:
    """The preferred display name for an email address.

    ``Alice.Smith+walk@x.com`` becomes ``Alice_Smith_walk``, which is both
    recognisable to the owner and free of the tag that would otherwise leak
    their provider.
    """
    local_part = email.split("@", 1)[0]
    candidate = sanitise(local_part)
    if len(candidate) < DISPLAY_NAME_MIN_LENGTH:
        # "a@b.com" sanitises to "a", which is too short to be a name. The
        # uniqueness retry will disambiguate the fallback if it collides.
        return FALLBACK_STEM
    return candidate


def random_suffix() -> str:
    """A short random suffix, from :mod:`secrets` rather than :mod:`random`.

    The suffix is not a security token, but ``random`` is a Mersenne Twister
    whose state is recoverable from observed output, and there is no reason to
    reach for it when ``secrets.choice`` costs the same.
    """
    return "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(SUFFIX_LENGTH))


def with_suffix(stem: str) -> str:
    """``alice`` -> ``alice_7f3a``, still within the length limit.

    The stem is trimmed *before* the suffix is appended so the result can never
    exceed :data:`DISPLAY_NAME_MAX_LENGTH` -- a name that overflows would be
    rejected by the column and turn a cosmetic collision into a signup failure.
    """
    suffix = random_suffix()
    room = DISPLAY_NAME_MAX_LENGTH - len(suffix) - 1
    return f"{stem[:room]}_{suffix}"


def candidate_for_attempt(stem: str, attempt: int) -> str:
    """The display name to try on a given signup attempt.

    Attempt 0 is the plain stem so that a player whose email is distinctive
    gets a clean name, and only collisions pay for a suffix.
    """
    if attempt <= 0:
        return stem
    if attempt >= DISPLAY_NAME_MAX_ATTEMPTS:
        raise ValueError(
            f"no display name available after {DISPLAY_NAME_MAX_ATTEMPTS} attempts"
        )
    return with_suffix(stem)
