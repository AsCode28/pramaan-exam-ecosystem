"""Explicit, demo-only reset of the local prototype database.

DESTRUCTIVE BY DESIGN. This drops and recreates every table so the deterministic
demo walkthrough can be repeated without manually deleting the database file.

Safety rules
------------
- Refuses to run unless ``DEMO_MODE`` is explicitly enabled.
- Refuses to run against a non-SQLite database URL: this is a prototype reset,
  not a production migration tool. A PostgreSQL/Redis deployment must be reset
  by its own operational process, never by an HTTP endpoint.
- Never invoked automatically. The walkthrough only calls it when the operator
  passes an explicit flag.
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from app.core.database import DATABASE_URL, engine
from app.core.demo_config import is_demo_mode
from app.db.base import Base


class DemoResetError(RuntimeError):
    """Reset refused (demo mode off, or an unsupported database URL)."""


def _assert_local_sqlite() -> None:
    url = (DATABASE_URL or "").lower()
    if not url.startswith("sqlite"):
        raise DemoResetError(
            "reset is only supported for the local SQLite prototype "
            f"(DATABASE_URL is {DATABASE_URL!r}); reset a production database "
            "through its own operational process, never over HTTP"
        )


def reset_demo_database(db: DbSession) -> dict:
    """Drop and recreate all tables. Only allowed for local SQLite in demo mode.

    Returns a small report. Never resets unless BOTH demo mode is on and the
    database is SQLite.
    """
    if not is_demo_mode():
        raise DemoResetError(
            "reset requires DEMO_MODE to be enabled; refusing to wipe data"
        )
    _assert_local_sqlite()

    # Release connection state before dropping metadata.
    db.rollback()
    db.close()

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    return {
        "reset": True,
        "database_scheme": DATABASE_URL.split(":", 1)[0],
        "tables_recreated": sorted(Base.metadata.tables),
    }
