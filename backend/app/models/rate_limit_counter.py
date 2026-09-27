"""``rate_limit_counters`` table -- request throttling state.

One row per ``(subject, scope)``. ``subject`` is whoever the limit is counted
against (a user id, or a client address for the unauthenticated endpoints) and
``scope`` is which limit it belongs to, so the two dimensions never collide: the
same subject can be counted separately for claims, for login, and for signup
without any of them interfering.

Deliberately *not* modelled as a game entity. There is no relationship to
``User`` and no cascade, because a rate-limit row outliving its subject is
harmless (worst case the subject is throttled for one window after deleting
their account) while a foreign key would mean every rate-limited request takes a
lock on a users row it does not otherwise need. Keeping it independent also
means the table survives a user deletion without a cleanup job.

The window is anchored on the first request in it rather than on wall-clock
minutes, which is what keeps this to one row per subject. See the reasoning in
``alembic/versions/0004_rate_limit_counters.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateLimitCounter(Base):
    __tablename__ = "rate_limit_counters"

    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)

    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    """When the current window opened. Reset to "now" when a request arrives
    after the window has expired, which is what makes the count below mean
    "requests since this instant" rather than "requests ever"."""

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("count >= 0", name="ck_rate_limit_counters_count"),
        # Supports the only cleanup query that exists: dropping rows whose
        # window closed long ago, so the table tracks active subjects.
        Index("ix_rate_limit_counters_window_started_at", "window_started_at"),
    )
