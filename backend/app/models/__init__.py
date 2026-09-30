"""Model registry.

Importing this package registers every model on ``Base.metadata``. Alembic's
``env.py`` and the test fixtures both rely on that happening exactly once, so
keep the imports explicit here rather than letting callers discover models by
accident.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.college import College
from app.models.hex_tile import HexTile
from app.models.rate_limit_counter import RateLimitCounter
from app.models.user import User
from app.models.walk_progress import WalkProgress

__all__ = ["Base", "College", "HexTile", "RateLimitCounter", "User", "WalkProgress"]
