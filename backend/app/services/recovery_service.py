"""Node-recovery + buffered-event reconciliation service (Task 5).

Recovery flow::

    FAILED --recover--> HEALTHY  (node; FAILED -> HEALTHY only)
    DISCONNECTED --recover--> RECOVERING --reconcile complete--> ACTIVE

``recover_node()`` flips the node and its DISCONNECTED sessions with the
same single-commit pattern as fail/start (flush, no commit, then
append_event() commits flips + event together).

``reconcile_session()`` accepts client-buffered ANSWER_SAVED events (only)
for a RECOVERING session, classifies each submitted client_event_id
exactly (newly_reconciled / already_acknowledged / missing / mismatch /
rejected), repairs the Response projection from authoritative events, and
transitions RECOVERING -> ACTIVE plus SESSION_RECOVERED only when the
strict completion rule holds.

The Response projection upsert itself lives in
``app.services.response_projection`` and is shared with Task 3's
``session_service.save_answer()`` (single source of truth for the guard
``last_event_id IS NULL OR last_event_id < sequence_no``).

Key integrity rules (plan Task 5 + revisions R1-R5, F1, F2, T1-T3):
- server_by_key snapshot is advisory; result.appended is authoritative.
- append_event() exceptions are AMBIGUOUS: re-query (session_id,
  client_event_id); only a confirmed absence is "ledger_append_failed".
- identical payload -> already_acknowledged + projection repair, no new row.
- conflicting payload -> mismatch; Event and Response never overwritten.
- same-request duplicate with different payload -> mismatch, never append.
- projection failure is per-item; batch continues; SESSION_RECOVERED only
  after every submitted key exists AND every Response is re-read current.

All event creation goes through append_event(); this module never writes
Event rows directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import (
    ForeignExamQuestion,
    InvalidNodeState,
    InvalidState,
    NotFound,
    NodeUnavailable,
)
from app.db.event import Event
from app.db.exam import Node, Question
from app.db.session import Response, Session
from app.services import event_ledger
from app.services.event_ledger import AppendResult
from app.services.response_projection import apply_answer_projection
from app.services.session_service import _get_or_404

NODE_RECOVERY_INITIATED = "NODE_RECOVERY_INITIATED"
SESSION_RECOVERED = "SESSION_RECOVERED"
ANSWER_SAVED = "ANSWER_SAVED"

FAILED = "FAILED"
HEALTHY = "HEALTHY"
ACTIVE = "ACTIVE"
DISCONNECTED = "DISCONNECTED"
RECOVERING = "RECOVERING"

RECOVERABLE_NODE_STATUSES = (FAILED,)


def _payload_matches(event: Event, question_id: int, answer: Any) -> bool:
    """Exact authoritative-payload equality on the two ANSWER_SAVED keys."""
    payload = event.payload or {}
    return (
        payload.get("question_id") == question_id and payload.get("answer") == answer
    )


def _fetch_winner(
    db: DbSession, *, session_id: int, client_event_id: str
) -> Event | None:
    """Fresh re-read of (session_id, client_event_id); defeats staleness."""
    db.expire_all()
    return (
        db.execute(
            select(Event).where(
                Event.session_id == session_id,
                Event.client_event_id == client_event_id,
            )
        ).scalar_one_or_none()
    )


def recover_node(
    db: DbSession, *, node_id: int, reason: str | None = None
) -> tuple[Node, list[int], AppendResult, bool]:
    """Recover a FAILED node to HEALTHY; DISCONNECTED sessions -> RECOVERING.

    Already-HEALTHY is idempotent (re-reports the original recovery event
    payload; no new event). Any other source status raises InvalidNodeState.
    Sessions never jump to ACTIVE here; that needs reconciliation.
    """
    node = db.get(Node, node_id)
    if node is None:
        raise NotFound("node")
    previous_status = node.status

    if previous_status == HEALTHY:
        existing = (
            db.execute(
                select(event_ledger.Event)
                .where(
                    event_ledger.Event.event_type == NODE_RECOVERY_INITIATED,
                    event_ledger.Event.node_id == node.id,
                )
                .order_by(event_ledger.Event.sequence_no.desc())
            )
            .scalars()
            .first()
        )
        if existing is None:  # pragma: no cover - defensive
            raise NotFound("node recovery event")
        affected_ids = list((existing.payload or {}).get("affected_session_ids", []))
        return node, affected_ids, AppendResult(event=existing, appended=False), False

    if previous_status not in RECOVERABLE_NODE_STATUSES:
        raise InvalidNodeState(node_id=node.id, status=previous_status)

    affected = (
        db.execute(
            select(Session.id)
            .where(Session.node_id == node.id, Session.status == DISCONNECTED)
            .order_by(Session.id)
        )
        .scalars()
        .all()
    )
    affected_ids = list(affected)

    node.status = HEALTHY
    if affected_ids:
        sessions = (
            db.execute(select(Session).where(Session.id.in_(affected_ids)))
            .scalars()
            .all()
        )
        for session in sessions:
            session.status = RECOVERING
    db.flush()  # NO commit; append_event() commits flips + event together

    result = event_ledger.append_event(
        db,
        event_type=NODE_RECOVERY_INITIATED,
        exam_id=node.exam_id,
        session_id=None,
        candidate_id=None,
        node_id=node.id,
        payload={
            "node_id": node.id,
            "previous_status": previous_status,
            "new_status": HEALTHY,
            "affected_session_ids": affected_ids,
            "affected_session_count": len(affected_ids),
            "reason": reason,
            "simulation": True,
        },
        client_event_id=None,
        client_timestamp=None,
    )
    return node, affected_ids, result, True


def _resolve_by_authoritative_payload(
    db: DbSession,
    *,
    session: Session,
    key: str,
    question_id: int,
    answer: Any,
    winner: Event,
    out: dict,
) -> None:
    """Classify a raced/ambiguous key against the authoritative winner."""
    if _payload_matches(winner, question_id, answer):
        try:
            ok = apply_answer_projection(
                db,
                session_id=session.id,
                question_id=question_id,
                answer=answer,
                sequence_no=winner.sequence_no,
            )
        except Exception:
            db.rollback()
            out["missing"].append(key)
            out["reasons"][key] = "projection_failed"
            out["projection_failed"] = True
            return
        if ok:
            out["already"].append(key)
            out["projected"][key] = True
        else:  # pragma: no cover - defensive; helper confirms on success
            out["missing"].append(key)
            out["reasons"][key] = "projection_failed"
            out["projection_failed"] = True
    else:
        out["mismatched"].append(key)


def _validate_item(db: DbSession, session: Session, key: str, qid: Any) -> str | None:
    """Return a rejection reason, or None when the item is valid."""
    question = db.get(Question, qid) if isinstance(qid, int) else None
    if question is None:
        return "unknown-question"
    if question.exam_id != session.exam_id:
        return "foreign-exam-question"
    return None


def _project_or_missing(
    db: DbSession, session: Session, key: str, qid: int, answer: Any,
    sequence_no: int, out: dict, bucket: str,
) -> None:
    """Run the shared upsert; record success or per-item projection miss."""
    try:
        ok = apply_answer_projection(
            db, session_id=session.id, question_id=qid,
            answer=answer, sequence_no=sequence_no,
        )
    except Exception:
        db.rollback()
        out["missing"].append(key)
        out["reasons"][key] = "projection_failed"
        out["projection_failed"] = True
        return
    if ok:
        out[bucket].append(key)
        out["projected"][key] = True
    else:
        out["missing"].append(key)
        out["reasons"][key] = "projection_failed"
        out["projection_failed"] = True

def reconcile_session(
    db: DbSession,
    *,
    session_id: int,
    items: list[dict],
) -> dict:
    """Reconcile buffered ANSWER_SAVED items for a RECOVERING session.

    Returns a report dict with exact sets (submitted/acknowledged/newly/
    already/missing/mismatched/rejected + reasons) and
    reconciliation_complete. Transitions to ACTIVE + SESSION_RECOVERED only
    when the strict completion rule holds (verified, not assumed).
    """
    session = _get_or_404(db, Session, session_id, "session")
    if session.status != RECOVERING:
        raise InvalidState(session.status)
    if session.node.status == FAILED:
        raise NodeUnavailable(
            node_id=session.node_id, session_id=session.id, operation="reconcile"
        )

    # Advisory snapshot only; result.appended decides races authoritatively.
    server_by_key = {
        e.client_event_id: e
        for e in db.execute(
            select(Event).where(
                Event.session_id == session.id,
                Event.client_event_id.is_not(None),
            )
        )
        .scalars()
        .all()
    }

    out: dict = {
        "submitted": [], "newly": [], "already": [], "missing": [],
        "mismatched": [], "rejected": [], "reasons": {}, "projected": {},
        "reference": {}, "ledger_failed": False, "projection_failed": False,
    }

    for raw in items:
        key = raw.get("client_event_id")
        qid = raw.get("question_id")
        answer = raw.get("answer")
        cts = raw.get("client_timestamp")
        if not key:
            continue
        if key in out["reference"]:
            ref = out["reference"][key]
            if ref != (qid, answer):
                # Conflicting duplicate in the SAME request: mismatch.
                if key not in out["mismatched"]:
                    out["mismatched"].append(key)
            elif out["projected"].get(key) is False:
                winner = _fetch_winner(db, session_id=session.id, client_event_id=key)
                if winner is not None and _payload_matches(winner, qid, answer):
                    was_missing = key in out["missing"]
                    _project_or_missing(
                        db, session, key, qid, answer,
                        winner.sequence_no, out, "already",
                    )
                    if was_missing and out["projected"].get(key) is True:
                        out["missing"].remove(key)
                        out["reasons"].pop(key, None)
            continue
        out["submitted"].append(key)
        out["reference"][key] = (qid, answer)
        out["projected"][key] = False

        reason = _validate_item(db, session, key, qid)
        if reason is not None:
            out["rejected"].append(key)
            out["reasons"][key] = reason
            continue

        existing = server_by_key.get(key)
        if existing is not None:
            if _payload_matches(existing, qid, answer):
                _project_or_missing(
                    db, session, key, qid, answer,
                    existing.sequence_no, out, "already",
                )
            else:
                out["mismatched"].append(key)
            continue

        try:
            result = event_ledger.append_event(
                db,
                event_type=ANSWER_SAVED,
                exam_id=session.exam_id,
                session_id=session.id,
                candidate_id=session.candidate_id,
                node_id=session.node_id,
                payload={"question_id": qid, "answer": answer},
                client_event_id=key,
                client_timestamp=cts,
            )
        except Exception:
            # AMBIGUOUS: re-query; only confirmed absence is ledger failure.
            winner = _fetch_winner(db, session_id=session.id, client_event_id=key)
            if winner is None:
                out["missing"].append(key)
                out["reasons"][key] = "ledger_append_failed"
                out["ledger_failed"] = True
            else:
                _resolve_by_authoritative_payload(
                    db, session=session, key=key, question_id=qid,
                    answer=answer, winner=winner, out=out,
                )
            continue

        if result.appended:
            _project_or_missing(
                db, session, key, qid, answer,
                result.event.sequence_no, out, "newly",
            )
        else:
            # Race lost: resolve against the authoritative winner.
            _resolve_by_authoritative_payload(
                db, session=session, key=key, question_id=qid,
                answer=answer, winner=result.event, out=out,
            )

    return _finalize_reconciliation(db, session, out)


def _finalize_reconciliation(db: DbSession, session: Session, out: dict) -> dict:
    """Verify completion by re-read; append SESSION_RECOVERED only if clean."""
    db.expire_all()
    final_events = {
        e.client_event_id: e
        for e in db.execute(
            select(Event).where(
                Event.session_id == session.id,
                Event.client_event_id.is_not(None),
            )
        )
        .scalars()
        .all()
    }
    acknowledged = sorted(final_events, key=lambda k: final_events[k].sequence_no)
    projections_ok = True
    for key in out["submitted"]:
        if key in out["mismatched"] or key in out["rejected"]:
            continue
        ev = final_events.get(key)
        if ev is None or not _payload_matches(ev, *out["reference"][key]):
            projections_ok = False
            break
        resp = db.execute(
            select(Response).where(
                Response.session_id == session.id,
                Response.question_id == out["reference"][key][0],
            )
        ).scalar_one_or_none()
        if (
            resp is None
            or resp.last_event_id is None
            or resp.last_event_id < ev.sequence_no
        ):
            projections_ok = False
            break

    # A clean run with ZERO buffered events is a successful recovery: there is
    # nothing to reconcile, so the session must not be stranded in RECOVERING.
    # Every other strict condition still has to hold, so mismatches, rejected
    # items, ledger failures and projection failures keep the session put.
    complete = (
        not out["mismatched"]
        and not out["rejected"]
        and not out["ledger_failed"]
        and not out["projection_failed"]
        and all(k in final_events for k in out["submitted"])
        and projections_ok
    )

    status = session.status
    recovered_seq: int | None = None
    if complete:
        session.status = ACTIVE
        db.flush()  # NO commit; SESSION_RECOVERED commits both together
        reconciled_ids = [k for k in out["submitted"] if k in final_events]
        proof = event_ledger.append_event(
            db,
            event_type=SESSION_RECOVERED,
            exam_id=session.exam_id,
            session_id=session.id,
            candidate_id=session.candidate_id,
            node_id=session.node_id,
            payload={
                "session_id": session.id,
                "reconciled_event_ids": reconciled_ids,
                "missing_count": 0,
                "mismatch_count": 0,
                "resulting_status": ACTIVE,
            },
            client_event_id=None,
            client_timestamp=None,
        )
        recovered_seq = proof.event.sequence_no
        status = ACTIVE

    return {
        "session": session,
        "status": status,
        "submitted": out["submitted"],
        "acknowledged": acknowledged,
        "newly": out["newly"],
        "already": out["already"],
        "missing": out["missing"],
        "mismatched": out["mismatched"],
        "rejected": out["rejected"],
        "reasons": out["reasons"],
        "complete": complete,
        "recovered_event_sequence_no": recovered_seq,
    }