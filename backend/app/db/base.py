"""Declarative base for all StepRealm models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for every ORM model.

    Kept in its own module with no imports from ``app.models`` so that
    ``alembic/env.py`` can import it without triggering a circular import.
    """
