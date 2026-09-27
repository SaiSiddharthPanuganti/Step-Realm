"""Request throttling: the limiter itself, and the routes that depend on it.

The first test in each section is the one that matters most. A limiter whose
upsert is broken does not raise -- it fails open, by design -- so the only
symptom is an absence, and a suite that checks "requests still work" is
indistinguishable from one where nothing is being limited at all. Every test
here therefore asserts on a request being *refused*, not merely permitted.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enforce(session, *, subject="user:1", scope="test", limit=3, window=60, now=None):
    from app.services import rate_limit_service

    return rate_limit_service.enforce(
        session,
        subject=subject,
        scope=scope,
        limit=limit,
        window_seconds=window,
        now=now,
    )


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


def test_limiter_refuses_the_request_past_the_limit(session) -> None:
    """The load-bearing test: a limit of 3 must actually refuse the 4th call.

    Written as a straight read of the specification. An earlier version of the
    upsert used ``text()`` fragments inside ``on_conflict_do_update``, whose bind
    parameters SQLAlchemy never compiled; the resulting error was swallowed by
    the fail-open handler, so every request was allowed and the entire 298-test
    suite passed against a limiter that was not limiting anything.
    """
    allowed = [
        _enforce(session, limit=3, now=datetime(2026, 1, 1, tzinfo=timezone.utc)).allowed
        for _ in range(3)
    ]
    blocked = _enforce(session, limit=3, now=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert allowed == [True, True, True]
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.retry_after_seconds == 60


def test_limiter_reports_remaining_budget(session) -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    remaining = [_enforce(session, limit=3, now=now).remaining for _ in range(3)]

    assert remaining == [2, 1, 0]


def test_limiter_stays_broken_down_after_the_limit(session) -> None:
    """Once refused, every later call in the same window is refused too."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        _enforce(session, limit=3, now=now)

    assert [_enforce(session, limit=3, now=now).allowed for _ in range(5)] == [
        False
    ] * 5


def test_limiter_window_expiry_restarts_the_count(session) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        _enforce(session, limit=3, now=start)
    assert _enforce(session, limit=3, now=start).allowed is False

    # One second past the window: the old allowance is gone and a fresh one
    # begins, so this is the *first* request of the next window, not the fourth.
    fresh = _enforce(session, limit=3, now=start + timedelta(seconds=61))

    assert fresh.allowed is True
    assert fresh.remaining == 2


def test_limiter_window_expiry_is_not_early(session) -> None:
    """The window must still stand one second before it closes.

    The boundary is where an off-by-one halves the effective limit without any
    visible symptom, so it is pinned from both sides: blocked just before the
    window ends, allowed at the moment it does.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(3):
        _enforce(session, limit=3, now=start)

    just_before = _enforce(session, limit=3, now=start + timedelta(seconds=59.9))
    at_boundary = _enforce(session, limit=3, now=start + timedelta(seconds=60))

    assert just_before.allowed is False
    assert at_boundary.allowed is True
    assert at_boundary.remaining == 2


def test_limiter_counts_scopes_independently(session) -> None:
    """One exhausted bucket must not refuse traffic in another."""
    from app.services import rate_limit_service

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        _enforce(session, scope=rate_limit_service.SCOPE_AUTH_LOGIN, limit=2, now=now)
    assert (
        _enforce(
            session, scope=rate_limit_service.SCOPE_AUTH_LOGIN, limit=2, now=now
        ).allowed
        is False
    )

    other = _enforce(
        session, scope=rate_limit_service.SCOPE_AUTH_SIGNUP, limit=2, now=now
    )

    assert other.allowed is True
    assert other.remaining == 1


def test_limiter_counts_subjects_independently(session) -> None:
    """One player hitting the wall does not throttle the next player."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        _enforce(session, subject="user:1", limit=2, now=now)

    assert _enforce(session, subject="user:1", limit=2, now=now).allowed is False
    assert _enforce(session, subject="user:2", limit=2, now=now).allowed is True


def test_limiter_counts_by_key_not_by_row_count(session) -> None:
    """A second subject adds a row; repeated hits on one subject do not."""
    from sqlalchemy import text as sql_text

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(6):
        _enforce(session, subject="user:1", limit=3, now=now)
    _enforce(session, subject="user:2", limit=3, now=now)

    rows = session.execute(
        sql_text("SELECT subject, count FROM rate_limit_counters ORDER BY subject")
    ).all()

    assert rows == [("user:1", 6), ("user:2", 1)]


def test_limiter_retry_after_shrinks_as_the_window_drains(session) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        _enforce(session, limit=2, now=start)

    early = _enforce(session, limit=2, now=start + timedelta(seconds=10))
    late = _enforce(session, limit=2, now=start + timedelta(seconds=50))

    assert early.retry_after_seconds == 50
    assert late.retry_after_seconds == 10


def test_limiter_retry_after_is_never_zero_when_refused(session) -> None:
    """A ``Retry-After: 0`` would invite an immediate retry and a hot loop."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(2):
        _enforce(session, limit=2, now=start)

    just_before = _enforce(session, limit=2, now=start + timedelta(seconds=59.99))

    assert just_before.allowed is False
    assert just_before.retry_after_seconds >= 1


def test_limiter_concurrent_calls_never_exceed_the_limit() -> None:
    """The upsert is the only thing preventing an over-admission race.

    Forty threads each opening their own session and asking for the same bucket
    with a limit of 10. Without the single atomic statement, read-modify-write
    interleaving would let well over ten through.
    """
    from app.db.session import get_session_factory

    limit = 10
    contenders = 40
    barrier = __import__("threading").Barrier(contenders)

    def _attempt(_: int) -> bool:
        barrier.wait(timeout=30)
        with get_session_factory()() as thread_session:
            return _enforce(thread_session, subject="user:race", limit=limit).allowed

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        outcomes = list(pool.map(_attempt, range(contenders)))

    assert sum(outcomes) == limit
    assert outcomes.count(False) == contenders - limit


def test_limiter_fails_open_when_the_counter_table_is_broken(
    session, monkeypatch
) -> None:
    """A limiter outage must not take the game down.

    The point of failing open is that a request still succeeds when counting is
    impossible, so the assertion is that this returns an *allow* rather than
    raising -- the opposite of what most of this module asserts.
    """
    from sqlalchemy.exc import OperationalError

    def _explode(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("database is down"))

    monkeypatch.setattr(session, "execute", _explode)

    decision = _enforce(session, limit=1)

    assert decision.allowed is True
    assert decision.remaining == 1
    assert decision.retry_after_seconds == 0


def test_limiter_fail_open_leaves_the_session_usable(session) -> None:
    """A real database error must not poison the limiter's session.

    The error is genuine rather than simulated. ``rate_limit_counters.subject``
    is ``String(255)``, so an over-long subject makes Postgres raise and really
    abort the transaction. Raising a synthetic exception from a monkeypatched
    ``execute`` does not exercise the same path at all.

    The assertion is that the *next* call both succeeds and persists its count.
    That is the property ``enforce``'s rollback exists to provide, and it is
    what stops a transient database error from silently disabling the limiter
    for the rest of the connection's life.
    """
    from sqlalchemy import text as sql_text

    # Fails for real: the subject is longer than the column.
    assert _enforce(session, subject="x" * 300, limit=3).allowed is True

    decision = _enforce(session, subject="user:1", limit=3)
    assert decision.allowed is True
    rows = session.execute(
        sql_text("SELECT subject, count FROM rate_limit_counters ORDER BY subject")
    ).all()
    assert rows == [("user:1", 1)]


def test_a_failed_count_leaves_no_partial_row(session) -> None:
    """The aborted statement must not leave a half-written counter behind.

    A row with a count but no window start would be read as an expired window on
    the next request, handing the subject a fresh allowance every time it hit an
    error -- a limiter that resets itself precisely when it is needed most.
    """
    from sqlalchemy import text as sql_text

    _enforce(session, subject="x" * 300, limit=3)

    rows = session.execute(
        sql_text("SELECT count(*) FROM rate_limit_counters")
    ).scalar()

    assert rows == 0


def test_limiter_fail_open_is_logged_loudly(session, monkeypatch, caplog) -> None:
    """A guard failing silently is indistinguishable from no guard at all."""
    import logging

    from sqlalchemy.exc import OperationalError

    def _explode(*args, **kwargs):
        raise OperationalError("UPDATE", {}, Exception("down"))

    monkeypatch.setattr(session, "execute", _explode)

    with caplog.at_level(logging.ERROR, logger="app.services.rate_limit_service"):
        _enforce(session, limit=1)

    assert any("fail-open" in record.message for record in caplog.records)


def test_purge_expired_drops_only_irrelevant_rows(session) -> None:
    """Reaping must not delete a window that can still refuse a request."""
    from app.services import rate_limit_service

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    _enforce(session, subject="user:fresh", limit=5, now=now)
    _enforce(session, subject="user:stale", limit=5, now=now - timedelta(days=1))

    removed = rate_limit_service.purge_expired(session, now=now)

    from sqlalchemy import text as sql_text

    remaining = session.execute(
        sql_text("SELECT subject FROM rate_limit_counters")
    ).scalars().all()
    assert removed == 1
    assert remaining == ["user:fresh"]


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_is_throttled_and_says_how_long_to_wait(
    client, settings_override
) -> None:
    settings_override(AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=3)
    payload = {"password": "correct-horse-battery-staple"}

    statuses = [
        client.post(
            "/auth/signup", json={**payload, "email": f"w{index}@example.com"}
        ).status_code
        for index in range(5)
    ]

    assert statuses == [201, 201, 201, 429, 429]


def test_signup_429_carries_retry_after(client, settings_override) -> None:
    settings_override(AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1)
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )

    refused = client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
    )

    assert refused.status_code == 429
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.headers["X-RateLimit-Limit"] == "1"
    assert refused.headers["X-RateLimit-Remaining"] == "0"


def test_a_refused_signup_creates_nothing(client, settings_override) -> None:
    """The throttle must run before the handler, not after the insert."""
    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    settings_override(AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1)
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )
    client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
    )

    with get_session_factory()() as read_session:
        emails = read_session.execute(
            sql_text("SELECT email FROM users ORDER BY email")
        ).scalars().all()

    assert emails == ["a@example.com"]


def test_a_refused_attempt_cannot_become_valid_later(client, settings_override) -> None:
    """Refusing signup must not leave a half-made account to log in with.

    This is the whole reason the limiter is a dependency rather than a check
    inside the handler: the username and the rate counter advance together.
    """
    from app.services.auth_service import normalise_email

    settings_override(AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1)
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )
    client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
    )

    response = client.post(
        "/auth/login",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
    )

    assert response.status_code == 401
    assert normalise_email("b@example.com") not in response.text


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_is_throttled_independently_of_signup(client, settings_override) -> None:
    """Exhausting the login budget must not refuse new registrations.

    Separate scopes are what make this true, and they are the reason
    ``scope`` is part of the primary key rather than implied by the route.
    """
    settings_override(
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=2, AUTH_LOGIN_RATE_LIMIT_PER_MINUTE=1
    )
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )
    credentials = {"email": "a@example.com", "password": "correct-horse-battery-staple"}

    login_statuses = [
        client.post("/auth/login", json=credentials).status_code for _ in range(3)
    ]

    assert login_statuses[0] == 200
    assert login_statuses[1:] == [429, 429]
    # Signup is untouched by all that login traffic.
    assert (
        client.post(
            "/auth/signup",
            json={
                "email": "c@example.com",
                "password": "correct-horse-battery-staple",
            },
        ).status_code
        == 201
    )


def test_login_throttle_survives_a_wrong_password(client, settings_override) -> None:
    """Credential stuffing is the attack this limit exists for.

    The counter has to advance on failures -- an implementation that only
    counted successes would be defeated by guessing.
    """
    settings_override(AUTH_LOGIN_RATE_LIMIT_PER_MINUTE=2)
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )

    statuses = [
        client.post(
            "/auth/login", json={"email": "a@example.com", "password": "wrong"}
        ).status_code
        for _ in range(3)
    ]

    assert statuses == [401, 401, 429]


# ---------------------------------------------------------------------------
# Trust boundary on the client address
# ---------------------------------------------------------------------------


def test_forwarded_for_is_ignored_by_default(client, settings_override) -> None:
    """An untrusted header must not be able to mint a fresh allowance.

    If it were honoured, forging a different address per request would reset the
    counter every time and the limit would be worth nothing.
    """
    settings_override(
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1, TRUST_FORWARDED_FOR="false"
    )
    client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
    )

    for index in range(4):
        response = client.post(
            "/auth/signup",
            json={
                "email": f"forged{index}@example.com",
                "password": "correct-horse-battery-staple",
            },
            headers={"X-Forwarded-For": f"10.0.0.{index}"},
        )
        assert response.status_code == 429, f"request {index} slipped through"


def test_forwarded_for_separates_addresses_when_trusted(
    client, settings_override
) -> None:
    """Behind a trusted proxy, distinct clients get distinct budgets.

    This is the reason ``trust_forwarded_for`` exists at all: without it, every
    player behind the load balancer shares one pool and the limit becomes a
    global cap on the platform.
    """
    settings_override(
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1, TRUST_FORWARDED_FOR="true"
    )

    first = client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "10.0.0.1"},
    )
    second = client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )
    same_again = client.post(
        "/auth/signup",
        json={"email": "c@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "10.0.0.1"},
    )

    assert [first.status_code, second.status_code, same_again.status_code] == [
        201,
        201,
        429,
    ]


def test_trusted_proxy_uses_the_leftmost_address(client, settings_override) -> None:
    """Proxies append, so the leftmost entry is the client the edge saw.

    The discriminating case is two requests that share a *rightmost* entry and
    differ on the left. Reading the header right-to-left maps both onto the
    nearest proxy, which is identical for every player, so the whole platform
    shares one bucket and the limit becomes a global cap. Sending the same
    header twice cannot tell the two readings apart -- both refuse the second
    request either way.
    """
    settings_override(
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1, TRUST_FORWARDED_FOR="true"
    )

    first = client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )
    # A different client, same edge proxy in the rightmost position.
    other_client = client.post(
        "/auth/signup",
        json={"email": "b@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "203.0.113.8, 10.0.0.1"},
    )
    # The same client again, now over budget.
    same_client = client.post(
        "/auth/signup",
        json={"email": "c@example.com", "password": "correct-horse-battery-staple"},
        headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"},
    )

    assert [first.status_code, other_client.status_code, same_client.status_code] == [
        201,
        201,
        429,
    ]


def test_malformed_forwarded_header_falls_back_to_the_peer(
    client, settings_override
) -> None:
    """A junk header must land on the same bucket as sending no header at all.

    The discriminating case mixes a headerless request with a whitespace-only
    one. The fallback has to be the peer address for the two to share a bucket;
    any other placeholder (``""``, or trusting the junk) gives the second request
    a brand new allowance, and a forged blank header becomes a way to reset the
    limit at will.
    """
    settings_override(
        AUTH_SIGNUP_RATE_LIMIT_PER_MINUTE=1, TRUST_FORWARDED_FOR="true"
    )
    password = {"password": "correct-horse-battery-staple"}

    no_header = client.post(
        "/auth/signup", json={**password, "email": "a@example.com"}
    )
    blank_header = client.post(
        "/auth/signup",
        json={**password, "email": "b@example.com"},
        headers={"X-Forwarded-For": "   "},
    )
    no_header_again = client.post(
        "/auth/signup", json={**password, "email": "c@example.com"}
    )

    # b shares a's bucket and is refused; c shares it too.
    assert [no_header.status_code, blank_header.status_code] == [201, 429]
    assert no_header_again.status_code == 429


# ---------------------------------------------------------------------------
# Per-user limits
# ---------------------------------------------------------------------------


def _funded(client, email: str, mana: int = 200) -> dict[str, str]:
    """Sign up over HTTP and set a mana balance. Returns auth headers.

    Mirrors ``tests/test_territory.py::player``. Funding the balance matters
    even though mana is not what these tests are about: a claim with an empty
    bar is refused with 402, which would arrive as an expected-looking status and
    quietly stop being evidence about throttling.
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.user import User

    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "correct-horse-battery-staple"},
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == email))
        user.mana = mana
        # Rewind the regen watermark, or settling would add mana on top.
        user.last_mana_regen_at = _dt.now(_tz.utc)
        session.commit()
    return headers


def _claim_at(client, headers, lat: float, lon: float = 0.0):
    return client.post(
        "/territory/claim-by-location", json={"lat": lat, "lon": lon}, headers=headers
    )


def test_claim_by_location_is_throttled_per_user(client, settings_override) -> None:
    """The world-probe limit: many claims, far fewer than a script needs.

    A client walking a real route produces a claim every few seconds, so the
    default budget is generous for a human and tight for something trying every
    tile in range to map who owns what.

    One player throughout, because the limit is per user -- spreading the
    requests across five accounts would give five separate budgets and prove
    nothing. Each request reclaims the same tile: that is a real 200 in the game
    (reclaiming your own tile is free and idempotent), so mana and the adjacency
    rule cannot masquerade as throttling.
    """
    settings_override(CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE=3)
    headers = _funded(client, "probe@example.com")

    statuses = [_claim_at(client, headers, 10.0).status_code for _ in range(5)]

    assert statuses == [200, 200, 200, 429, 429]


def test_two_players_have_separate_claim_budgets(client, settings_override) -> None:
    """A per-*IP* limit here would be a denial-of-service on a shared network.

    Two accounts on one address must each get their own allowance; a wifi cafe
    full of players would otherwise exhaust a single shared bucket and all of
    them would find claiming impossible.
    """
    settings_override(CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE=1)
    first = _funded(client, "one@example.com")
    second = _funded(client, "two@example.com")

    # Each player's first claim is their own first claim, so the game rules
    # allow it and the throttle is the only variable.
    a = _claim_at(client, first, 10.0)
    b = _claim_at(client, second, 20.0)
    a_again = _claim_at(client, first, 10.0)

    assert [a.status_code, b.status_code, a_again.status_code] == [200, 200, 429]


def test_an_unauthenticated_caller_is_401_and_spends_nothing(
    client, settings_override
) -> None:
    """A 401 must not consume a real player's allowance.

    FastAPI solves a dependency's sub-dependencies before running its body, so
    the ``get_current_user`` that raises 401 means the throttling body never
    runs. Throttling first would let anyone who knows another player's token
    burn their budget, and would report a 429 to a client whose real problem is
    a stale token.
    """
    settings_override(CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE=2)
    headers = _funded(client, "solo@example.com")

    anonymous = _claim_at(client, {}, 10.0)
    # Reclaiming the same tile is free in mana and free of the adjacency rule,
    # so the only limit that can refuse these is the throttle.
    claims = [_claim_at(client, headers, 10.0).status_code for _ in range(3)]

    assert anonymous.status_code == 401
    # Budget intact: two claims succeed, the third is throttled.
    assert claims == [200, 200, 429]


def test_a_throttled_claim_changes_no_world_state(client, settings_override) -> None:
    """The refused claim must not leave a tile behind.

    The dependency raises before the handler runs, so there is nothing to roll
    back -- but the assertion is worth having, because a limiter implemented
    inside the handler after the insert would pass every other test in this
    file while leaving a permanent, mana-less row behind.
    """
    from sqlalchemy import text as sql_text

    from app.db.session import get_session_factory

    settings_override(CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE=1)
    headers = _funded(client, "solo@example.com")
    _claim_at(client, headers, 10.0)

    refused = _claim_at(client, headers, 20.0)

    with get_session_factory()() as read_session:
        count = read_session.execute(sql_text("SELECT count(*) FROM hex_tiles")).scalar()

    assert refused.status_code == 429
    assert count == 1


def test_claim_throttle_429_carries_retry_after(client, settings_override) -> None:
    settings_override(CLAIM_LOCATION_RATE_LIMIT_PER_MINUTE=1)
    headers = _funded(client, "solo@example.com")
    _claim_at(client, headers, 10.0)

    refused = _claim_at(client, headers, 20.0)

    assert refused.status_code == 429
    assert 1 <= int(refused.headers["Retry-After"]) <= 60
    assert refused.headers["X-RateLimit-Limit"] == "1"
    assert refused.headers["X-RateLimit-Remaining"] == "0"
