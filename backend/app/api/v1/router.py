"""Aggregate router for the public API surface.

Each phase appends its router here. Kept as one explicit list (rather than
auto-discovery) so the public surface is readable in a single place and
import errors surface at startup instead of on first request.

Paths are mounted unprefixed -- POST /auth/signup, GET /me -- to match the
API surface in the spec exactly, so the mobile client is written against the
documented paths rather than a /api/v1 prefix the spec never mentions.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, mana, me, territory, world

router = APIRouter()

router.include_router(auth.router)
router.include_router(mana.router)
router.include_router(me.router)
router.include_router(world.router)
router.include_router(territory.router)

# Phase 7: college
# Phase 8: leaderboard
