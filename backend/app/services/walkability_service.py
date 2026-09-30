"""Which hexes sit on walkable ground, answered from OpenStreetMap.

The MVP plane is procedural: every hex inside a viewport is returned, and any
hex adjacent to your territory can be claimed regardless of what is actually
underneath it. A walk*ing* game wants territory only where a player can walk.
This module answers "which of these hexes is walkable" by asking Overpass which
walkable ways and parks pass through the viewport, then snapping *samples* of
that geometry down to the hexes they fall in.

Trade-offs, stated so they are not surprises later:

* The check is best-effort against a public, third-party service. A fetch
  failure raises :class:`WalkabilityUnavailable` and the caller decides what
  that means -- serve the plain plane (fail open) or refuse claims (fail
  closed). "Overpass is down" is an availability event, not a gameplay datum,
  so this module refuses to guess which the caller wants.
* Everything is decided from OpenStreetMap tags, so a place OSM has not mapped
  well comes back non-walkable. That is the honest reading of "only walkable
  areas get hexes": uncertain ground reads as no ground, rather than as
  claimable.
* Results are cached per bounding box snapped to a small grid, because a map
  screen polls every 20 s and pans a few hundred metres at a time. The whole
  point of the world fetch is that most viewports overlap the last one, and
  hitting Overpass for every one of them would be rude.

Network access lives here and only here. Neither ``geo`` nor ``world_service``
imports this module; ``world_service.build_viewport`` takes an injected
resolver so the pure geometry path stays testable without an HTTP client.
"""

from __future__ import annotations

import json
import logging
import math
import ssl
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterable
from threading import Lock

from app.core.config import get_settings
from app.core.constants import (
    HEX_SIZE_METERS,
    WALKABLE_HIGHWAY_TAGS,
    WALKABLE_LEISURE_TAGS,
    WALKABILITY_SAMPLE_SPACING_METERS,
)
from app.services.geo import (
    Axial,
    lonlat_to_axial,
    tiles_bounding_box,
    tiles_in_radius,
)

logger = logging.getLogger(__name__)

#: A point in ``(lon, lat)``, the order Overpass hands geometry to us in and
#: the order RFC 7946 uses, so it never has to be renamed on the way in.
Point = tuple[float, float]
#: A walkable way or area ring, as its vertices.
Geometry = list[Point]

_USER_AGENT = "steprealm-dev/0.1 (walkability-classifier)"

#: ``(cell key) -> (monotonic time, walkable set)``. Both members guarded by
#: :data:`_CACHE_LOCK`. A plain dict rather than anything clever: the set for a
#: grid cell is bounded (a few thousand hexes) and a lost race just fetches
#: twice.
_cache: dict[tuple[int, int, int, int], tuple[float, frozenset[Axial]]] = {}
_cache_lock = Lock()


class WalkabilityUnavailable(Exception):
    """Overpass was unreachable, answered with an error, or the answer was
    unusable. The caller decides what failure this maps to."""


def _tls_context() -> ssl.SSLContext:
    """A context that trusts certifi's Mozilla bundle when present.

    Python's default context trusts the OS store, which on some Windows
    installs fails to resolve the Let's Encrypt chain that Overpass serves
    (it reports an expired certificate even though every other client agrees
    it is current). certifi ships an up-to-date bundle, so preferring it when
    available makes the client work identically everywhere. Falls back to the
    default context otherwise; losing the extra bundle is not a security
    change, it is just accepting the platform's own trust store.
    """
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def haversine_meters(a: Point, b: Point) -> float:
    """Ground distance between two close ``(lon, lat)`` points, in metres.

    Only ever used to *space samples*, so the spherical approximation is
    plenty -- the error is a fraction of a percent at neighbourhood scale.
    """
    lat_a = math.radians(a[1])
    lat_b = math.radians(b[1])
    dlat = lat_b - lat_a
    dlon = math.radians(b[0] - a[0])
    # sin²(Δ/2) form of the haversine law. Writing it out rather than pulling
    # in geopy keeps this module a stdlib-only file, which is also the file
    # whose runtime behaviour matters most on a dev machine.
    h = math.sin(dlat / 2.0) ** 2 + math.cos(lat_a) * math.cos(lat_b) * math.sin(
        dlon / 2.0
    ) ** 2
    return 2.0 * 6371008.8 * math.asin(math.sqrt(h))


def _sample_segment(a: Point, b: Point, spacing: float) -> Iterable[Point]:
    """Every ``spacing`` metres along ``a -> b``, endpoints included.

    Linear interpolation in degrees rather than an actual geodesic walk. The
    segments we interpolate are at most a few hundred metres long, so the
    chord-vs-arc error is millimetres at the scales that matter, and linear
    interpolation is exactly reproducible in a test without a great-circle
    library.
    """
    distance = haversine_meters(a, b)
    steps = max(1, math.ceil(distance / spacing))
    for index in range(steps + 1):
        t = index / steps
        yield a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def classify_walkable_hexes(
    geometries: Iterable[Geometry], size: float = HEX_SIZE_METERS
) -> set[Axial]:
    """Map walkable geometry to the set of hexes it passes through.

    A hex is walkable when a sample of walkable geometry falls inside it. The
    whole sampling business exists because OSM node density is not uniform: a
    node every 5 m on a winding footpath and one every 300 m down a long
    straight residential road are both normal, and snapping nodes alone would
    punch holes in the second case. Resampling every
    ``WALKABILITY_SAMPLE_SPACING_METERS`` along each segment guarantees the
    road's every hex is hit.

    ``lonlat_to_axial`` here is the *same* projection ``GET /world`` uses to
    place its tiles, so a hex is walkable iff the server would have drawn it
    under the road. Reimplementation is the one bug this codebase treats as
    epidemic, so there is none: we call the shared module.
    """
    walkable: set[Axial] = set()
    spacing = WALKABILITY_SAMPLE_SPACING_METERS
    for geometry in geometries:
        vertices = len(geometry)
        if vertices == 0:
            continue
        if vertices < 2:
            # A degenerate way (Overpass normally guarantees two or more, but a
            # malformed reply costs nothing to tolerate): its one point still
            # names the hex it sits in.
            lon, lat = geometry[0]
            walkable.add(lonlat_to_axial(lat, lon, size))
            continue
        for a, b in zip(geometry, geometry[1:]):
            for lon, lat in _sample_segment(a, b, spacing):
                walkable.add(lonlat_to_axial(lat, lon, size))
    return walkable


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------


def _bbox_for(disc: list[Axial], size: float = HEX_SIZE_METERS) -> tuple[float, float, float, float]:
    """``(south, west, north, east)`` for an exact hex disc.

    The order is Overpass's, not ours: it is the one API in this file that
    demands latitude-first. Getting it backwards looks correct on the wire and
    misses every way, so it is worth the explicit docstring.
    """
    bounds = tiles_bounding_box(disc, size)
    return bounds.min_lat, bounds.min_lon, bounds.max_lat, bounds.max_lon


def _fetch_interpreter(
    request: urllib.request.Request, deadline_seconds: float
) -> dict:
    """POST a query to Overpass and collect the JSON within a wall-clock budget.

    ``urlopen(timeout=...)`` only bounds each individual socket operation, not
    the total exchange: an endpoint that trickles bytes every couple of seconds
    is "alive" to a socket timeout and yet takes 18 s to answer a question that
    ``/world`` cannot hold its response hostage to. So the fetch runs on a
    daemon thread and is *abandoned*, rather than cancelled, when the deadline
    passes -- abandoned matters, because there is no safe way to interrupt a
    blocked socket from another thread. The stranded thread keeps waiting until
    Overpass finishes on its own and then quietly dies; daemon means it never
    holds up interpreter shutdown.
    """
    collected: list[dict] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            with urllib.request.urlopen(
                request,
                timeout=deadline_seconds,
                context=_tls_context(),
            ) as response:
                collected.append(json.load(response))
        except BaseException as exc:  # noqa: BLE001 -- thread boundary; surfaced below
            failure.append(exc)

    worker = threading.Thread(target=run, daemon=True, name="steprealm-overpass")
    worker.start()
    worker.join(timeout=deadline_seconds)
    if worker.is_alive():
        raise WalkabilityUnavailable(
            f"Overpass did not answer within {deadline_seconds:.0f}s"
        )
    if failure:
        raise WalkabilityUnavailable(f"Overpass request failed: {failure[0]}") from failure[0]
    try:
        return collected[0]
    except IndexError as exc:  # pragma: no cover -- run() always records one or the other
        raise WalkabilityUnavailable("Overpass answered with an empty document") from exc


def _overpass_query(bbox: tuple[float, float, float, float]) -> list[Geometry]:
    """Fetch the walkable geometries inside a bounding box.

    Two statements unioned: highways with a walkable ``highway`` tag, and
    ``leisure=park`` polygons (a park is the one walkable area with no road
    running through it). ``out geom`` inlines each way's vertex geometry so the
    response needs no second node lookup round trip.
    """
    settings = get_settings()
    south, west, north, east = bbox
    highways = "|".join(sorted(WALKABLE_HIGHWAY_TAGS))
    leisure = "|".join(sorted(WALKABLE_LEISURE_TAGS))
    query = (
        f"[out:json][timeout:{int(settings.walkability_timeout_seconds)}];\n"
        "(\n"
        f'  way["highway"~"^({highways})$"]({south},{west},{north},{east});\n'
        f'  way["leisure"~"^({leisure})$"]({south},{west},{north},{east});\n'
        ");\n"
        "out geom;"
    )

    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    request = urllib.request.Request(
        settings.walkability_overpass_url,
        data=payload,
        headers={"User-Agent": _USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
    )

    body = _fetch_interpreter(request, settings.walkability_timeout_seconds)

    elements = body.get("elements", [])
    if not isinstance(elements, list):
        raise WalkabilityUnavailable(
            "Overpass answered with an unexpected document"
        )

    geometries: list[Geometry] = []
    for element in elements:
        if element.get("type") != "way" or "geometry" not in element:
            continue
        tags = element.get("tags") or {}
        if tags.get("highway") not in WALKABLE_HIGHWAY_TAGS and tags.get(
            "leisure"
        ) not in WALKABLE_LEISURE_TAGS:
            # The Overpass query already filters; this guard keeps the parser
            # honest if the endpoint ever answers a wider query.
            continue
        points = [(vertex["lon"], vertex["lat"]) for vertex in element["geometry"]]
        if points:
            geometries.append(points)
    return geometries


def _cell_key(bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """Snap a bounding box to the cache grid.

    Four degrees of freedom quantised so that two viewports whose boxes share a
    cell reuse one fetch: the same roads, the same hexes, and a cache entry
    worth keeping. A pan of less than a cell width (about 2 km) is still
    answering from the same terrain.
    """
    cell = get_settings().walkability_cache_cell_degrees

    def quantise(value: float) -> int:
        return round(value / cell)

    south, west, north, east = bbox
    return quantise(south), quantise(west), quantise(north), quantise(east)


def walkability_for_disc(
    disc: list[Axial], size: float = HEX_SIZE_METERS
) -> set[Axial]:
    """The walkable subset of an exact hex disc.

    Raising :class:`WalkabilityUnavailable` when Overpass cannot answer, so the
    caller can decide what a dead dependency means instead of this module
    guessing.
    """
    settings = get_settings()
    bbox = _bbox_for(disc, size)
    key = _cell_key(bbox)
    now = time.monotonic()

    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < settings.walkability_cache_ttl_seconds:
            return set(cached[1])

    geometries = _overpass_query(bbox)
    walkable = classify_walkable_hexes(geometries, size)

    with _cache_lock:
        _cache[key] = (now, frozenset(walkable))
    return walkable


def walkability_for_claim(q: int, r: int, size: float = HEX_SIZE_METERS) -> bool:
    """Whether one hex is walkable, for a claim check.

    A deliberately small disc (rings=1) around the target: the only question a
    claim asks is about one tile, and a fetch covering seven hexes instead of
    the whole viewport is a fetch a fraction of the size.
    """
    disc = list(tiles_in_radius(q, r, 1))
    return (q, r) in walkability_for_disc(disc, size)