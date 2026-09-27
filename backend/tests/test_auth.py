"""Signup, login, and protected-route behaviour.

These run against a real PostgreSQL (steprealm_test) via the real app and
real sessions -- no dependency overrides -- so what is under test is what
ships.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select

from app.core.config import get_settings
from app.core.constants import INITIAL_MANA
from app.models.user import User
from tests.conftest import VALID_EMAIL, VALID_PASSWORD


# ---------------------------------------------------------------------------
# signup
# ---------------------------------------------------------------------------


def test_signup_creates_account_and_returns_token(client) -> None:
    response = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in_seconds"] > 0
    assert body["user"]["email"] == VALID_EMAIL
    # A new player can afford exactly one tile immediately.
    assert body["user"]["mana"] == INITIAL_MANA
    assert body["user"]["owned_tile_count"] == 0
    assert body["user"]["college"] is None


def test_signup_never_returns_password_material(client) -> None:
    body = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
    ).json()
    serialised = str(body)
    assert "password" not in serialised.lower()
    assert "$2b$" not in serialised


def test_signup_stores_only_a_bcrypt_digest(session) -> None:
    client_ok = client_post_signup()
    assert client_ok == 201

    user = session.scalar(select(User).where(User.email == VALID_EMAIL))
    assert user is not None
    assert user.password_hash != VALID_PASSWORD
    assert VALID_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$2b$")


def client_post_signup() -> int:
    """Helper that posts a signup using a throwaway client."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        return c.post(
            "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
        ).status_code


def test_signup_lowercases_and_strips_email(client, session) -> None:
    response = client.post(
        "/auth/signup",
        json={"email": "  Walker@Example.COM  ", "password": VALID_PASSWORD},
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["email"] == VALID_EMAIL

    user = session.scalar(select(User).where(User.email == VALID_EMAIL))
    assert user is not None


def test_signup_rejects_duplicate_email_case_insensitively(client) -> None:
    assert (
        client.post(
            "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
        ).status_code
        == 201
    )
    duplicate = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
    )
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"].lower()


def test_signup_rejects_short_password(client) -> None:
    response = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": "short"}
    )
    assert response.status_code == 422


def test_signup_rejects_password_over_72_bytes(client) -> None:
    """The limit is on UTF-8 *bytes*, not characters.

    30 emoji are 30 characters but 120 bytes, so a character-based cap would
    let this through and bcrypt would silently truncate it.
    """
    emoji_password = "\U0001f600" * 30
    assert len(emoji_password) == 30
    assert len(emoji_password.encode("utf-8")) == 120

    response = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": emoji_password}
    )
    assert response.status_code == 422
    assert "72 bytes" in response.text


def test_signup_accepts_password_at_the_byte_limit(client) -> None:
    # 24 four-byte characters is exactly 96; use ASCII to hit exactly 72.
    response = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": "a" * 72}
    )
    assert response.status_code == 201, response.text


def test_signup_rejects_malformed_email(client) -> None:
    response = client.post(
        "/auth/signup", json={"email": "not-an-email", "password": VALID_PASSWORD}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_with_correct_credentials(client) -> None:
    client.post("/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD})

    response = client.post(
        "/auth/login", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
    )
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def test_login_is_case_insensitive_on_email(client) -> None:
    client.post("/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD})

    response = client.post(
        "/auth/login", json={"email": "WALKER@EXAMPLE.COM", "password": VALID_PASSWORD}
    )
    assert response.status_code == 200, response.text


def test_login_with_wrong_password_is_401(client) -> None:
    client.post("/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD})

    response = client.post(
        "/auth/login", json={"email": VALID_EMAIL, "password": "wrong-password-x"}
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_login_with_unknown_email_is_indistinguishable_from_wrong_password(
    client,
) -> None:
    """Both failures must be byte-identical responses, or login becomes an
    account-enumeration oracle."""
    client.post("/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD})

    wrong_password = client.post(
        "/auth/login", json={"email": VALID_EMAIL, "password": "wrong-password-x"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "wrong-password-x"}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.headers.get("WWW-Authenticate") == (
        unknown_email.headers.get("WWW-Authenticate")
    )


def test_login_does_not_accept_a_short_password_by_length_alone(client) -> None:
    """Login must not apply signup's policy rules.

    If login rejected short passwords structurally it would confirm the guess
    was well-formed rather than merely wrong.
    """
    client.post("/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD})
    response = client.post(
        "/auth/login", json={"email": VALID_EMAIL, "password": "tiny"}
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# token shape
# ---------------------------------------------------------------------------


def test_token_carries_the_required_claims(client) -> None:
    body = client.post(
        "/auth/signup", json={"email": VALID_EMAIL, "password": VALID_PASSWORD}
    ).json()

    settings = get_settings()
    payload = jwt.decode(
        body["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )

    assert set(payload) >= {"sub", "type", "iat", "nbf", "exp"}
    assert payload["type"] == "access"
    assert payload["sub"] == str(body["user"]["id"])
    assert payload["nbf"] == payload["iat"]
    assert payload["exp"] > payload["iat"]


def test_token_signed_with_a_different_secret_is_rejected(client) -> None:
    import jwt as pyjwt

    now = datetime.now(timezone.utc)
    forged = pyjwt.encode(
        {
            "sub": "1",
            "type": "access",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        "a-completely-different-signing-key-value-32b",
        algorithm="HS256",
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_token_with_wrong_type_claim_is_rejected(client, register) -> None:
    """A future refresh token must not be replayable as an access token."""
    import jwt as pyjwt

    body = register()
    settings = get_settings()
    now = datetime.now(timezone.utc)
    wrong_type = pyjwt.encode(
        {
            "sub": body["user"]["id"],
            "type": "refresh",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {wrong_type}"})
    assert response.status_code == 401


def test_token_with_alg_none_is_rejected(client, register) -> None:
    """Guards the classic algorithm-confusion attack."""
    import jwt as pyjwt

    body = register()
    now = datetime.now(timezone.utc)
    unsigned = pyjwt.encode(
        {
            "sub": body["user"]["id"],
            "type": "access",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {unsigned}"})
    assert response.status_code == 401


def test_expired_token_is_rejected(client, register) -> None:
    import jwt as pyjwt

    body = register()
    settings = get_settings()
    issued = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = pyjwt.encode(
        {
            "sub": body["user"]["id"],
            "type": "access",
            "iat": int(issued.timestamp()),
            "nbf": int(issued.timestamp()),
            "exp": int((issued + timedelta(hours=1)).timestamp()),
        },
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get("/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_token_naming_a_deleted_user_is_rejected(client, register, session) -> None:
    headers = auth_headers_of(register())
    user_id = client.get("/me", headers=headers).json()["id"]

    from app.db.session import get_session_factory

    with get_session_factory()() as write_session:
        victim = write_session.get(User, user_id)
        write_session.delete(victim)
        write_session.commit()

    response = client.get("/me", headers=headers)
    assert response.status_code == 401


def auth_headers_of(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


# ---------------------------------------------------------------------------
# protected route
# ---------------------------------------------------------------------------


def test_me_requires_a_token(client) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_me_rejects_garbage_token(client) -> None:
    response = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_me_rejects_malformed_authorization_header(client) -> None:
    for header in ("", "Bearer", "Basic abcdef", "bearer", "Token abc"):
        response = client.get("/me", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_me_returns_the_current_user(client, register) -> None:
    body = register()
    response = client.get("/me", headers=auth_headers_of(body))
    assert response.status_code == 200, response.text

    payload = response.json()
    assert payload["id"] == body["user"]["id"]
    assert payload["email"] == VALID_EMAIL
    assert payload["mana"] == INITIAL_MANA
    assert payload["owned_tile_count"] == 0
    assert payload["college"] is None
    assert "password" not in str(payload).lower()
