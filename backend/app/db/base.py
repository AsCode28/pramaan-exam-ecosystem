"""Shared SQLAlchemy declarative base for all PRAMAAN ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single declarative base; every ORM table registers on Base.metadata."""
