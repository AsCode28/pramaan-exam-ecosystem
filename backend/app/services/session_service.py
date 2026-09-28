"""Session business logic (no HTTP knowledge).

All event creation goes through ``append_event()``; this module never
writes Event rows directly. The Event ledger is the historical source of
truth and a committed event is never rolled back conceptually.

``Response.last_event_id`` is an integer projection pointer storing
``Event.sequence_no`` (NOT an ``Event.id``; the events table has no ``id``
column). The projection guard is::

    Response.last_event_id IS NULL OR Response.last_event_id < Event.sequence_no

/start transaction design (single atomic commit):
  validate -> add Session (flush only, NO commit) -> append_event()
  (which commits internally). The Session row and the SESSION_STARTED event
  therefore become durable together; if the append fails before commit, the
  rollback removes the uncommitted Session row too, leaving no orphan.

Answer/heartbeat design (event-first):
  validate -> append_event() (owns its commit) -> projection update + commit.
  If the projection update fails, the event stands and the error propagates
  with a ``projection_failed`` flag; the projection is recoverable by
  replaying ANSWER_SAVED events in sequence_no order.

Idempotency conflict rule: a retry reusing (session_id, client_event_id)
with a DIFFERENT payload returns the original event; the projection keeps
reflecting the first-appended authoritative event and is not overwritten.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import ForeignExamQuestion, InvalidState, NotFound, NodeUnavailable
from app.db.candidate import Candidate
from app.db.event import Event
from app.db.exam import Exam, Node, Question
from app.db.session import Response, Session
from app.services import event_ledger, response_projection
from app.services.event_ledger import AppendResult

SESSION_STARTED = "SESSION_STARTED"
ANSWER_SAVED = "ANSWER_SAVED"
HEARTBEAT = "HEARTBEAT"

# Session states that can never be followed by another live session for the
# same (exam, candidate). A candidate either is still sitting the exam
# (ACTIVE / DISCONNECTED / RECOVERING) or has finished; re-starting must not
# fork a second concurrent session.
TERMINAL_SESSION_STATUSES = ("SUBMITTED", "COMPLETED", "TERMINATED")


def _utcnow_naive() -> datetime:
    """Backend-authoritative timestamp (SQLite stores naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _get_or_404(db: DbSession, model: Any, id_: int, entity: str) -> Any:
    row = db.get(model, id_)
    if row is None:
        raise NotFound(entity)
    return row


class NodeExamMismatch(ForeignExamQuestion):
    """Node belongs to a different exam than the requested session exam."""

    def __init__(self):
        Exception.__init__(self, "node belongs to a different exam")


def _reject_if_node_unavailable(session: Session, operation: str) -> None:
    """Backend-authoritative failed-node guard (runs before append_event())."""
    if session.status == "DISCONNECTED" or session.node.status == "FAILED":
        raise NodeUnavailable(
            node_id=session.node_id, session_id=session.id, operation=operation
        )
    if session.status == "RECOVERING" and operation == "answer":
        # Normal answers stay closed during recovery; use reconcile instead.
        raise InvalidState(session.status)


def find_live_session(
    db: DbSession, *, exam_id: int, candidate_id: int
) -> Session | None:
    """Existing non-terminal session for this (exam, candidate), if any.

    ACTIVE / DISCONNECTED / RECOVERING all count as live: the candidate is
    still in that sitting, so a repeated start must be served idempotently
    rather than forking a second concurrent session. A submitted session is
    terminal and is never returned.
    """
    return db.execute(
        select(Session)
        .where(
            Session.exam_id == exam_id,
            Session.candidate_id == candidate_id,
            Session.status.not_in(TERMINAL_SESSION_STATUSES),
        )
        .order_by(Session.id.asc())
        .limit(1)
    ).scalar_one_or_none()


def start_session(
    db: DbSession, *, exam_id: int, candidate_id: int, node_id: int
) -> tuple[Session, AppendResult]:
    """Validate, create the Session (uncommitted), append SESSION_STARTED.

    A repeated start for the same (exam, candidate) returns the existing
    non-terminal session with ``appended=False`` and appends no second
    SESSION_STARTED, so the ledger keeps exactly one start per sitting.
    """
    exam = _get_or_404(db, Exam, exam_id, "exam")
    candidate = _get_or_404(db, Candidate, candidate_id, "candidate")
    node = _get_or_404(db, Node, node_id, "node")
    if node.exam_id != exam.id:
        raise NodeExamMismatch()
    if node.status == "FAILED":
        # Never start a session on a known-dead node.
        raise NodeUnavailable(node_id=node.id, session_id=None, operation="start")

    # Idempotent repeat: the candidate is already sitting this exam.
    existing = find_live_session(db, exam_id=exam.id, candidate_id=candidate.id)
    if existing is not None:
        start_event = db.execute(
            select(Event)
            .where(
                Event.session_id == existing.id,
                Event.event_type == SESSION_STARTED,
            )
            .order_by(Event.sequence_no.asc())
            .limit(1)
        ).scalar_one_or_none()
        if start_event is not None:
            return existing, AppendResult(event=start_event, appended=False)
        # No ledger proof of the original start (pre-existing data): keep the
        # existing session and report it without inventing an event.
        return existing, AppendResult(event=start_event, appended=False)

    now = _utcnow_naive()
    session = Session(
        exam_id=exam.id,
        candidate_id=candidate.id,
        node_id=node.id,
        status="ACTIVE",
        started_at=now,
        last_activity=now,
    )
    db.add(session)
    db.flush()  # assigns session.id; NO commit here
    result = event_ledger.append_event(
        db,
        event_type=SESSION_STARTED,
        exam_id=exam.id,
        session_id=session.id,
        candidate_id=candidate.id,
        node_id=node.id,
        payload={},
        client_event_id=None,
        client_timestamp=None,
    )
    return session, result


def save_answer(
    db: DbSession,
    *,
    session_id: int,
    question_id: int,
    answer: str | None,
    client_event_id: str,
    client_timestamp: datetime | None,
) -> tuple[Response, AppendResult]:
    """Append ANSWER_SAVED, then upsert the Response projection."""
    session = _get_or_404(db, Session, session_id, "session")
    _reject_if_node_unavailable(session, "answer")
    question = _get_or_404(db, Question, question_id, "question")
    if question.exam_id != session.exam_id:
        raise ForeignExamQuestion()
    if session.status != "ACTIVE":
        raise InvalidState(session.status)

    result = event_ledger.append_event(
        db,
        event_type=ANSWER_SAVED,
        exam_id=session.exam_id,
        session_id=session.id,
        candidate_id=session.candidate_id,
        node_id=session.node_id,
        payload={"question_id": question.id, "answer": answer},
        client_event_id=client_event_id,
        client_timestamp=client_timestamp,
    )
    event = result.event
    try:
        # Shared projection helper (single source of truth for the upsert);
        # guard: apply iff last_event_id IS NULL OR last_event_id < sequence_no.
        response_projection.apply_answer_projection(
            db,
            session_id=session.id,
            question_id=question.id,
            answer=event.payload["answer"],
            sequence_no=event.sequence_no,
        )
        response = response_projection.get_response_row(
            db, session_id=session.id, question_id=question.id
        )
    except Exception as exc:
        db.rollback()
        # The ANSWER_SAVED event from append_event() stands (authoritative);
        # only the projection write failed. Signal replayability upstream.
        exc.projection_failed = True  # type: ignore[attr-defined]
        raise
    return response, result


def record_heartbeat(
    db: DbSession,
    *,
    session_id: int,
    client_event_id: str | None,
) -> tuple[Session, AppendResult]:
    """Append HEARTBEAT and advance last_activity with backend time.

    Idempotent-retry rule: when append_event() returns appended=False, the
    heartbeat was already recorded, so last_activity is NOT modified; the
    existing event acknowledgement and existing session state are returned.
    """
    session = _get_or_404(db, Session, session_id, "session")
    _reject_if_node_unavailable(session, "heartbeat")
    result = event_ledger.append_event(
        db,
        event_type=HEARTBEAT,
        exam_id=session.exam_id,
        session_id=session.id,
        candidate_id=session.candidate_id,
        node_id=session.node_id,
        payload={},
        client_event_id=client_event_id,
        client_timestamp=None,
    )
    if not result.appended:
        # Idempotent retry: leave last_activity untouched; return the
        # already-recorded acknowledgement with current session state.
        db.refresh(session)
        return session, result
    if session.status == "RECOVERING":
        # Recovery is not write-ready: append the liveness event but do NOT
        # advance last_activity (that would imply the session is usable).
        db.refresh(session)
        return session, result
    try:
        session.last_activity = _utcnow_naive()
        db.commit()
        db.refresh(session)
    except Exception as exc:
        db.rollback()
        exc.projection_failed = True  # type: ignore[attr-defined]
        raise
    return session, result


def get_session_state(db: DbSession, *, session_id: int) -> dict:
    """Read-only snapshot for frontend reconciliation (appends nothing)."""
    session = _get_or_404(db, Session, session_id, "session")
    responses = (
        db.execute(
            select(Response)
            .where(Response.session_id == session.id)
            .order_by(Response.question_id)
        )
        .scalars()
        .all()
    )
    # COMPLETE acknowledged set: every non-NULL client key for this session,
    # ordered canonically by sequence_no. Queried from events directly so
    # overwritten answers cannot drop earlier acknowledgements.
    acknowledged = (
        db.execute(
            select(Event.client_event_id)
            .where(
                Event.session_id == session.id,
                Event.client_event_id.is_not(None),
            )
            .order_by(Event.sequence_no)
        )
        .scalars()
        .all()
    )
    return {
        "session": session,
        "node_status": session.node.status,
        "responses": responses,
        "acknowledged_client_event_ids": list(acknowledged),
    }