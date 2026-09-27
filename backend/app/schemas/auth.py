"""Wire models for authentication."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.constants import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH
from app.schemas.user import UserResponse


def _validate_password(value: str) -> str:
    # bcrypt's limit is on encoded bytes, not characters. 30 emoji are 30
    # characters but 120 UTF-8 bytes, so a character-based max_length would
    # wave through a password that bcrypt silently truncates -- and silent
    # truncation means two different passwords can share a digest.
    encoded = len(value.encode("utf-8"))
    if encoded > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 "
            f"encoded (this one is {encoded})"
        )
    return value


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        """Lowercase and strip.

        The database enforces case-insensitive uniqueness via
        ``uq_users_email_lower``, but doing it here as well means what we
        store is what we compare against on login, so the plain index on
        ``users.email`` stays usable.
        """
        return value.strip().lower()

    @field_validator("password")
    @classmethod
    def _check_password_bytes(cls, value: str) -> str:
        return _validate_password(value)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()

    # Deliberately no min_length here. Rejecting a short password at login
    # would tell an attacker their guess failed for a structural reason
    # rather than a credential reason, and could lock out accounts created
    # before a policy change.


class TokenResponse(BaseModel):
    """Issued on both signup and login.

    ``user`` is included so the client can render the profile immediately
    instead of spending a round trip on GET /me right after authenticating.
    """

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in_seconds: int
    user: UserResponse


class MessageResponse(BaseModel):
    detail: str
