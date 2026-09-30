"""POST /territory/walk and the claim-distance gate it feeds.

Two halves, mirroring test_territory's split:

* The report half pins down the ledger itself: accumulation, server-side
  attribution, the per-report ceiling, the plausibility speed check, and auth.
* The gate half turns ``min_claim_distance_meters`` back on (conftest pins it
  to 0 for the walk-free claim suites) and proves a hex cannot be claimed until
  the player has banked enough metres *inside it* -- and that a successful claim
  spends those metres.

Booking the metres always goes over HTTP with a real token, and the balance is
seeded directly into the DB afterwards, exactly as test_territory does, so the
gate tests are about the gate and not about mana.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.core.constants import MAX_WALK_REPORT_METERS, TILE_CLAIM_COST
from app.db.session import get_session_factory
from app.models.walk_progress import WalkProgress
from app.models.user import User
from app.services.geo import lonlat_to_axial


def player(authed, email: str, mana: int) -> tuple[dict, int]:
    """Register over HTTP and set the mana balance. Returns (headers, user_id)."""
    headers = authed(email)
    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        user.mana = mana
        user.last_mana_regen_at = datetime.now(timezone.utc)
        session.commit()
        return headers, user.id


def report_walk(client, headers, lat: float, lon: float, meters: float):
    return client.post(
        "/territory/walk",
        json={"lat": lat, "lon": lon, "meters": meters},
        headers=headers,
    )


def claim(client, headers, q: int, r: int):
    return client.post("/territory/claim", json={"q": q, "r": r}, headers=headers)


def progress_rows(user_id: int) -> list[tuple[int, int, float]]:
    with get_session_factory()() as session:
        return list(
            session.execute(
                select(WalkProgress.q, WalkProgress.r, WalkProgress.meters).where(
                    WalkProgress.user_id == user_id
                )
            ).all()
        )


def age_walk_baseline(user_id: int, *, by: timedelta) -> None:
    """Backdate a user's walk-report baseline.

    The plausibility check measures a report's metres against the time since
    the previous report, and HTTP tests cannot pass a clock. Backdating the
    baseline simulates the seconds that pass between two real reports in a
    walking session -- exactly what ``age_step_sync`` does for the step suite.
    """
    with get_session_factory()() as session:
        session.execute(
            text(
                "UPDATE users SET last_walk_report_at = last_walk_report_at - "
                "(:interval)::interval WHERE id = :id"
            ),
            {"interval": f"{int(by.total_seconds())} seconds", "id": user_id},
        )
        session.commit()


def world_at(client, headers, lat: float, lon: float) -> dict:
    response = client.get(
        "/world", params={"lat": lat, "lon": lon, "rings": 1}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_walk_report_accumulates_per_hex(client, authed) -> None:
    headers, user_id = player(authed, "walker@example.com", TILE_CLAIM_COST)

    first = report_walk(client, headers, 0.0, 0.0, 10.0)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["q"] == 0 and body["r"] == 0
    assert body["total"] == 10.0
    assert body["required"] == 0.0
    assert body["remaining"] == 0.0

    # A real second report arrives seconds later, not microseconds. Backdating
    # the baseline makes the plausibility check measure the 10 simulated
    # seconds rather than the instant between HTTP calls.
    age_walk_baseline(user_id, by=timedelta(seconds=10))
    second = report_walk(client, headers, 0.0, 0.0, 12.5)
    assert second.status_code == 200, second.text
    assert second.json()["total"] == 22.5

    assert progress_rows(user_id) == [(0, 0, 22.5)]


def test_walk_report_attribution_is_projected_server_side(client, authed) -> None:
    headers, _user_id = player(authed, "walker2@example.com", TILE_CLAIM_COST)

    # A coordinate that should project to a hex other than the origin's.
    q, r = lonlat_to_axial(12.9716, 77.5946)
    response = report_walk(client, headers, 12.9716, 77.5946, 5.0)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["q"], body["r"]) == (q, r)
    assert body["total"] == 5.0


def test_walk_report_requires_a_token(client) -> None:
    response = client.post(
        "/territory/walk", json={"lat": 0.0, "lon": 0.0, "meters": 5.0}
    )
    assert response.status_code == 401


def test_walk_report_rejects_zero_and_oversized(client, authed) -> None:
    headers, _user_id = player(authed, "walker3@example.com", TILE_CLAIM_COST)

    zero = client.post(
        "/territory/walk", json={"lat": 0.0, "lon": 0.0, "meters": 0}, headers=headers
    )
    assert zero.status_code == 422

    huge = client.post(
        "/territory/walk",
        json={"lat": 0.0, "lon": 0.0, "meters": MAX_WALK_REPORT_METERS + 1},
        headers=headers,
    )
    assert huge.status_code == 422


def test_walk_report_rejects_implausible_speed(client, authed) -> None:
    headers, user_id = player(authed, "walker4@example.com", TILE_CLAIM_COST)

    # A real first report establishes the baseline at "now".
    first = report_walk(client, headers, 0.0, 0.0, 5.0)
    assert first.status_code == 200, first.text

    # An immediate second report of 40 m implies a sprint across a city block
    # in the milliseconds the two requests were apart. Refused, and the ledger
    # and baseline are untouched.
    rejected = report_walk(client, headers, 0.0, 0.0, 40.0)
    assert rejected.status_code == 422, rejected.text
    assert "not plausible" in rejected.json()["detail"].lower()

    assert progress_rows(user_id) == [(0, 0, 5.0)]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_claim_requires_walked_distance(
    client, authed, settings_override
) -> None:
    settings_override(min_claim_distance_meters=20)
    headers, user_id = player(authed, "gated@example.com", TILE_CLAIM_COST)

    refused = claim(client, headers, 0, 0)
    assert refused.status_code == 422, refused.text
    assert "Walk" in refused.json()["detail"]

    report = report_walk(client, headers, 0.0, 0.0, 20.0)
    assert report.status_code == 200, report.text
    assert report.json()["total"] == 20.0
    assert report.json()["remaining"] == 0.0

    accepted = claim(client, headers, 0, 0)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["tile"]["is_new"] is True

    # The metres that made the claim possible were spent with it.
    assert progress_rows(user_id) == []


def test_claim_by_location_requires_walked_distance(
    client, authed, settings_override
) -> None:
    settings_override(min_claim_distance_meters=20)
    headers, _user_id = player(authed, "gated2@example.com", TILE_CLAIM_COST)

    refused = client.post(
        "/territory/claim-by-location",
        json={"lat": 0.006, "lon": 0.006},
        headers=headers,
    )
    assert refused.status_code == 422, refused.text

    report = report_walk(client, headers, 0.006, 0.006, 20.0)
    assert report.status_code == 200, report.text

    accepted = client.post(
        "/territory/claim-by-location",
        json={"lat": 0.006, "lon": 0.006},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text


def test_partial_progress_does_not_unlock_a_claim(client, authed, settings_override) -> None:
    settings_override(min_claim_distance_meters=20)
    headers, _user_id = player(authed, "gated3@example.com", TILE_CLAIM_COST)

    report = report_walk(client, headers, 0.0, 0.0, 19.5)
    assert report.status_code == 200, report.text
    assert report.json()["remaining"] > 0

    refused = claim(client, headers, 0, 0)
    assert refused.status_code == 422, refused.text


# ---------------------------------------------------------------------------
# The map reflects the ledger
# ---------------------------------------------------------------------------


def test_world_exposes_viewer_progress(client, authed, settings_override) -> None:
    settings_override(min_claim_distance_meters=20)
    headers, _user_id = player(authed, "worldseen@example.com", TILE_CLAIM_COST)

    report_walk(client, headers, 0.0, 0.0, 20.0)

    body = world_at(client, headers, 0.0, 0.0)
    assert body["claim_required_meters"] == 20.0
    by_coord = {(t["q"], t["r"]): t for t in body["tiles"]}
    assert by_coord[(0, 0)]["walk_meters"] == 20.0

    # Another player sees the same world but not the first player's progress.
    other_headers, _other = player(authed, "other@example.com", TILE_CLAIM_COST)
    other = world_at(client, other_headers, 0.0, 0.0)
    other_by_coord = {(t["q"], t["r"]): t for t in other["tiles"]}
    assert other_by_coord[(0, 0)]["walk_meters"] == 0.0


def test_anonymous_world_does_not_leak_progress(client) -> None:
    response = client.get("/world", params={"lat": 0.0, "lon": 0.0, "rings": 1})
    assert response.status_code == 200, response.text
    for tile in response.json()["tiles"]:
        assert tile["walk_meters"] == 0.0


def test_own_tile_needs_no_progress(client, authed, settings_override) -> None:
    """Idempotent retries of a hex you own bypass the gate entirely."""
    settings_override(min_claim_distance_meters=20)
    headers, user_id = player(authed, "retry@example.com", TILE_CLAIM_COST)

    report_walk(client, headers, 0.0, 0.0, 20.0)
    first = claim(client, headers, 0, 0)
    assert first.status_code == 200, first.text

    retry = claim(client, headers, 0, 0)
    assert retry.status_code == 200, retry.text
    assert retry.json()["mana_spent"] == 0

    assert progress_rows(user_id) == []