"""Canonical Response projection helper (shared by Task 3 and Task 5).

Single home of the Response upsert logic. ``Response`` is a current-state
projection of the append-only Event ledger, so this helper only ever moves
the projection FORWARD:

    apply iff Response.last_event_id IS NULL
          OR Response.last_event_id < Event.sequence_no

``last_event_id`` is an integer projection pointer storing
``Event.sequence_no`` (NOT an ``Event.id``; the events table has no ``id``
column). Callers own the transaction boundary: this helper commits its own
write (matching the existing behaviour of both call sites) and returns
whether the row is confirmed up-to-date afterwards. It never creates or
mutates Event rows.

Concurrency
-----------
Advancement of ``last_event_id`` is serialised per
``(session_id, question_id)`` by :data:`_PROJECTION_LOCKS`, so two concurrent
writers can never let an older ``sequence_no`` overwrite a newer projection.
The UPDATE is additionally guarded in SQL
(``... WHERE last_event_id IS NULL OR last_event_id < :sequence_no``), so the
monotonic rule still holds even if a row were touched by a writer that did not
hold the lock. Concurrent first-time inserts are retried on the
``UNIQUE(session_id, question_id)`` conflict and then fall through to the
guarded UPDATE.

Like the ledger lock, this is **single-process** protection only.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.db.session import Response

# Per-projection-key locks: {"(session_id, question_id)": Lock}. A single global
# lock would be simpler but would serialise unrelated answers; a dict guarded by
# a creation lock keeps concurrency while staying deterministic.
_PROJECTION_LOCKS: dict[tuple[int, int], threading.Lock] = {}
_PROJECTION_LOCKS_GUARD = threading.Lock()


def _lock_for(session_id: int, question_id: int) -> threading.Lock:
    """Return the stable per-projection-key lock, creating it once."""
    key = (session_id, question_id)
    with _PROJECTION_LOCKS_GUARD:
        lock = _PROJECTION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROJECTION_LOCKS[key] = lock
        return lock


def _advance_projection(
    db: DbSession,
    *,
    session_id: int,
    question_id: int,
    answer: Any,
    sequence_no: int,
) -> bool:
    """Advance the projection without ever regressing ``last_event_id``.

    Returns True when the stored ``last_event_id`` is >= ``sequence_no``.
    """
    # Atomic, monotonic UPDATE: the guard lives in the SQL statement itself.
    result = db.execute(
        update(Response)
        .where(
            Response.session_id == session_id,
            Response.question_id == question_id,
            Response.last_event_id.is_(None) | (Response.last_event_id < sequence_no),
        )
        .values(current_answer=answer, last_event_id=sequence_no)
    )
    if result.rowcount:
        db.commit()
        return True

    # No row advanced: either it is absent, or it is already at/ahead of us.
    db.commit()
    row = db.execute(
        select(Response.last_event_id).where(
            Response.session_id == session_id,
            Response.question_id == question_id,
        )
    ).scalar_one_or_none()
    if row is None:
        # Genuinely absent even after the update: nothing to confirm.
        return False
    return row >= sequence_no


def apply_answer_projection(
    db: DbSession,
    *,
    session_id: int,
    question_id: int,
    answer: Any,
    sequence_no: int,
) -> bool:
    """Upsert the Response projection for (session_id, question_id).

    Semantics (unchanged from Task 3 / Task 5):
    - create the Response row when missing (``last_event_id = sequence_no``)
    - otherwise update only when ``last_event_id IS NULL`` or
      ``last_event_id < sequence_no`` (a stale replay never regresses)
    - commit and refresh
    - return True when the stored projection is confirmed up-to-date

    Raises on database failure; callers contain it (Task 5 flags the item as
    ``projection_failed`` and keeps the authoritative Event).
    """
    with _lock_for(session_id, question_id):
        # Try the cheap monotonic UPDATE first; it covers every existing row.
        result = db.execute(
            update(Response)
            .where(
                Response.session_id == session_id,
                Response.question_id == question_id,
                Response.last_event_id.is_(None)
                | (Response.last_event_id < sequence_no),
            )
            .values(current_answer=answer, last_event_id=sequence_no)
        )
        if result.rowcount:
            db.commit()
            return True

        # The row either already moved ahead of us, or does not exist yet.
        existing = db.execute(
            select(Response).where(
                Response.session_id == session_id,
                Response.question_id == question_id,
            )
        ).scalar_one_or_none()

        if existing is not None:
            # Already at or beyond sequence_no: a stale replay never regresses.
            db.commit()
            return existing.last_event_id is not None and (
                existing.last_event_id >= sequence_no
            )

        # First write for this key. Insert, tolerating a concurrent creator.
        response = Response(
            session_id=session_id,
            question_id=question_id,
            current_answer=answer,
            last_event_id=sequence_no,
        )
        db.add(response)
        try:
            db.commit()
        except IntegrityError:
            # Another writer created the row first (UNIQUE(session_id,
            # question_id)). Re-apply the monotonic rule against their row.
            db.rollback()
            return _advance_projection(
                db,
                session_id=session_id,
                question_id=question_id,
                answer=answer,
                sequence_no=sequence_no,
            )
        db.refresh(response)
        return response.last_event_id is not None and (
            response.last_event_id >= sequence_no
        )


def get_response_row(db: DbSession, *, session_id: int, question_id: int) -> Response | None:
    """Read-only fetch of the projection row (callers that need the row back)."""
    return db.execute(
        select(Response).where(
            Response.session_id == session_id,
            Response.question_id == question_id,
        )
    ).scalar_one_or_none()
