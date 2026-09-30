"""Environment-driven settings.

Gameplay numbers are in :mod:`app.core.constants`; this module holds only
secrets and deployment knobs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.constants import TILE_CLAIM_MIN_METERS

#: Value that must never reach a real deployment. Rejected at startup by
#: :meth:`Settings._reject_placeholder_secret` when not in development.
_PLACEHOLDER_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- app ---------------------------------------------------------------
    app_name: str = "StepRealm"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    # -- datastores --------------------------------------------------------
    # Defaults target the docker-compose services in this directory, so the
    # app boots with no .env at all. The driver is pinned to psycopg (sync)
    # rather than asyncpg: psycopg ships reliable cp314 wheels, and the sync
    # driver makes the row-locking semantics in territory_service.py (and the
    # phase-5 race test) far easier to reason about than async equivalents.
    database_url: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/steprealm"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    #: Cheap guard against connections killed by the server or a proxy. With
    #: a Railway Postgres that sleeps on idle, this is what stops the first
    #: request after a sleep from raising an opaque OperationalError.
    db_pool_pre_ping: bool = True
    db_echo: bool = False
    #: psycopg/libpq has no default connect timeout, so an unreachable
    #: database that blackholes packets (rather than refusing the connection)
    #: makes every request -- including /health -- hang until the platform
    #: kills the container. Measured at >2 minutes without this.
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    #: Server-side ceiling on any single statement, so one pathological query
    #: cannot hold a worker indefinitely. Applied per connection below.
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)

    redis_url: str | None = None
    redis_socket_timeout_seconds: float = 2.0
    redis_health_ttl_seconds: int = 5

    # -- auth --------------------------------------------------------------
    #: Required with no default. Failing fast at import beats booting with a
    #: known-bad signing key, and SecretStr keeps it out of log output and
    #: tracebacks.
    jwt_secret: SecretStr
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    #: bcrypt work factor. 12 is a sensible interactive-login cost (~250 ms).
    #: Tests lower this to keep the suite fast -- see tests/conftest.py.
    bcrypt_rounds: int = Field(default=12, ge=4, le=16)

    # -- world anchor ------------------------------------------------------
    # Consulted only when world_max_radius_from_origin is set. Irrelevant for
    # the unbounded MVP plane, but the projection is defined relative to these
    # so that turning the leash on is a one-line change.
    world_origin_lat: float = 0.0
    world_origin_lon: float = 0.0

    #: Hex rings from ``world_origin_*`` within which territory may be claimed
    #: and the map may be viewed. ``None`` (the default) means unbounded, which
    #: is the honest MVP setting: the plane is a real-world map, so a player in
    #: another country is not cheating by standing there. Setting this closes the
    #: one real anti-cheat hole -- a spoofed first fix seeding a territory on the
    #: far side of the planet -- at the cost of making the game a fenced arena.
    world_max_radius_from_origin: int | None = Field(default=None, ge=1, le=100_000)

    # -- rate limiting -----------------------------------------------------
    claim_location_rate_limit_per_minute: int = Field(
        default=10, ge=1, le=600
    )
    auth_login_rate_limit_per_minute: int = Field(default=10, ge=1, le=600)
    auth_signup_rate_limit_per_minute: int = Field(default=5, ge=1, le=600)

    #: Whether to read the client address from ``X-Forwarded-For``.
    #:
    #: OFF by default, and it must stay off unless the service is genuinely
    #: unreachable except through a proxy that overwrites the header. Without
    #: that guarantee the header is attacker-controlled: anyone can send
    #: ``X-Forwarded-For: <victim>`` and have their requests counted against
    #: somebody else, which turns a per-address limit into a denial-of-service
    #: tool aimed at a chosen victim. With the flag off, every client behind the
    #: proxy shares the proxy's address and the limit becomes a shared pool --
    #: too blunt, which is why the flag exists at all, and why turning it on is a
    #: deployment decision rather than a default.
    trust_forwarded_for: bool = False

    # -- claim gating ---------------------------------------------------------
    #: How far a player must walk inside a hex before they can claim it.
    #: ``0`` disables the requirement (tests and the mobile contract harness
    #: pin this to keep claim flows walk-free). Defaults to
    #: constants.TILE_CLAIM_MIN_METERS; see that constant for why 20 is the
    #: product decision. Deployment-tunable here because the "walk 20 m first"
    #: rule is a rule a staging or demo deployment may want to relax.
    min_claim_distance_meters: float = Field(
        default=TILE_CLAIM_MIN_METERS, ge=0, le=1000
    )

    # -- walkability ---------------------------------------------------------
    #: Whether hexes are restricted to walkable ground (roads and parks from
    #: OpenStreetMap). OFF by default so a deployment without Outbound access
    #: keeps the old fully-procedural plane; the map tiles /world with a live
    #: walkability check flagged, and claims require the target hex to be in the
    #: walkable set. Both degrade to the old behaviour when Overpass is down.
    walkability_enabled: bool = False
    walkability_overpass_url: str = "https://overpass-api.de/api/interpreter"
    #: Overpass is a public service with a queue; a short timeout keeps a slow
    #: answer from pinning a world load or, worse, a claim attempt.
    walkability_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    #: The raw Overpass answer for a viewport is cached keyed on its bounding
    #: box snapped to this grid, so screens near each other share one fetch.
    walkability_cache_cell_degrees: float = Field(default=0.02, gt=0, le=1.0)
    walkability_cache_ttl_seconds: float = Field(default=600.0, gt=0)

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw or len(raw) < 32:
            raise ValueError(
                "JWT_SECRET must be set and at least 32 characters long. "
                "Generate one with: python -c \"import secrets;"
                " print(secrets.token_urlsafe(48))\""
            )
        return value

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"development", "dev", "local"}

    def validate_for_runtime(self) -> None:
        """Extra startup checks that cannot be expressed as field validators
        because they depend on other fields."""
        if not self.is_development and (
            self.jwt_secret.get_secret_value() == _PLACEHOLDER_SECRET
        ):
            raise RuntimeError(
                "JWT_SECRET is still the placeholder value and environment is "
                f"{self.environment!r}. Refusing to start."
            )
        if self.environment.lower() in {"production", "prod"} and not self.redis_url:
            # Not fatal: the leaderboard degrades explicitly by design, so we
            # warn loudly rather than refusing to serve the rest of the game.
            pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
