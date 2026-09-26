"""Database engine / session setup for local development (SQLite).

``Base`` is imported through the ``app.db`` package so that all ORM model
modules are registered on ``Base.metadata`` before ``init_db()`` can run.
No migrations (Alembic) yet: ``init_db()`` creates all tables directly and
is intended for the local screening prototype only.
"""

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = BACKEND_DIR / "pramaan.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create all ORM tables (no-op for tables that already exist)."""
    Base.metadata.create_all(engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DATABASE_URL}")
    print(f"Tables: {sorted(Base.metadata.tables)}")
