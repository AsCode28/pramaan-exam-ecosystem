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
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db.session import Response


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
    response = db.execute(
        select(Response).where(
            Response.session_id == session_id,
            Response.question_id == question_id,
        )
    ).scalar_one_or_none()
    if response is None:
        response = Response(
            session_id=session_id,
            question_id=question_id,
            current_answer=answer,
            last_event_id=sequence_no,
        )
        db.add(response)
    elif response.last_event_id is None or response.last_event_id < sequence_no:
        response.current_answer = answer
        response.last_event_id = sequence_no
    db.commit()
    db.refresh(response)
    return response.last_event_id is not None and response.last_event_id >= sequence_no


def get_response_row(db: DbSession, *, session_id: int, question_id: int) -> Response | None:
    """Read-only fetch of the projection row (callers that need the row back)."""
    return db.execute(
        select(Response).where(
            Response.session_id == session_id,
            Response.question_id == question_id,
        )
    ).scalar_one_or_none()
