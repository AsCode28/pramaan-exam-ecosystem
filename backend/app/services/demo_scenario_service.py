"""Demo scenario provisioning and the read-only operational overview.

Both helpers are demo-scoped conveniences for the resilience walkthrough. They
never touch ledger, hashing, session, projection, recovery, reconciliation,
incident-detection, audit or AI-grounding semantics.

create_demo_scenario
--------------------
Creates exactly one exam, one HEALTHY node, three candidates and three
questions. It deliberately creates **no sessions**: sessions must still be
started through ``POST /session/start`` so that the ordinary
``SESSION_STARTED`` events are produced by the real code path. Nothing is
deleted or reset; the scenario is purely additive.

build_overview
--------------
Read-only operational snapshot of one exam: exam, nodes, sessions, incidents
(with evidence sequence numbers), the global audit verification result and the
latest ledger sequence number. It never calls Gemini.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession

from app.db.candidate import Candidate
from app.db.event import Event
from app.db.exam import Exam, Node, NodeStatus, Question
from app.db.incident import Incident, IncidentSession
from app.db.session import Session
from app.services import audit_service, incident_service

CANDIDATE_COUNT = 3
QUESTION_COUNT = 3

_HEALTHY = NodeStatus.HEALTHY.value


def _utcnow_naive() -> datetime:
    """Backend-authoritative timestamp (SQLite stores naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _unique_suffix(db: DbSession) -> str:
    """A short token that keeps demo roll numbers unique across scenarios."""
    latest = db.scalar(select(Candidate.id).order_by(desc(Candidate.id)).limit(1))
    return f"{(latest or 0) + 1:06d}"


def create_demo_scenario(db: DbSession) -> dict[str, Any]:
    """Create one exam + one HEALTHY node + 3 candidates + 3 questions.

    No Session rows and no Event rows are created: the demo must go through
    ``POST /session/start`` so the normal SESSION_STARTED path is exercised.
    """
    now = _utcnow_naive()
    suffix = _unique_suffix(db)

    exam = Exam(
        title=f"Demo Resilience Exam {suffix}",
        status="ACTIVE",
        start_time=now,
        end_time=now + timedelta(hours=3),
    )
    db.add(exam)
    db.flush()

    node = Node(exam_id=exam.id, status=_HEALTHY)
    db.add(node)
    db.flush()

    candidates = [
        Candidate(name=f"Demo Candidate {i + 1}", roll_no=f"DEMO-{suffix}-{i + 1}")
        for i in range(CANDIDATE_COUNT)
    ]
    db.add_all(candidates)

    questions = [
        Question(exam_id=exam.id, text=f"Demo question {i + 1}?", marks=1)
        for i in range(QUESTION_COUNT)
    ]
    db.add_all(questions)

    db.commit()

    return {
        "exam_id": exam.id,
        "node_id": node.id,
        "candidate_ids": [c.id for c in candidates],
        "question_ids": [q.id for q in questions],
    }


def build_overview(db: DbSession, exam_id: int) -> dict[str, Any] | None:
    """Read-only operational snapshot of one exam. Returns None if unknown."""
    exam = db.get(Exam, exam_id)
    if exam is None:
        return None

    nodes = db.scalars(
        select(Node).where(Node.exam_id == exam_id).order_by(Node.id.asc())
    ).all()
    sessions = db.scalars(
        select(Session)
        .where(Session.exam_id == exam_id)
        .order_by(Session.id.asc())
    ).all()
    incidents = db.scalars(
        select(Incident)
        .where(Incident.exam_id == exam_id)
        .order_by(Incident.id.asc())
    ).all()

    incident_rows = []
    for incident in incidents:
        # Reuse the incident engine's evidence selection (single source).
        evidence_sequence_nos = incident_service.get_incident_evidence(db, incident)
        affected_session_ids = db.scalars(
            select(IncidentSession.session_id)
            .where(IncidentSession.incident_id == incident.id)
            .order_by(IncidentSession.session_id.asc())
        ).all()
        incident_rows.append(
            {
                "id": incident.id,
                "severity": incident.severity,
                "status": incident.status,
                "root_cause_summary": incident.root_cause_summary,
                "created_from_event_id": incident.created_from_event_id,
                "detected_at": incident.detected_at,
                "resolved_at": incident.resolved_at,
                "affected_session_ids": list(affected_session_ids),
                "evidence_event_sequence_nos": evidence_sequence_nos,
            }
        )

    audit_result = audit_service.verify_ledger_chain(db)
    latest_sequence_no = db.scalar(select(Event.sequence_no).order_by(desc(Event.sequence_no)).limit(1))

    return {
        "exam": {
            "id": exam.id,
            "title": exam.title,
            "status": exam.status,
            "start_time": exam.start_time,
            "end_time": exam.end_time,
        },
        "nodes": [
            {"id": n.id, "exam_id": n.exam_id, "status": n.status} for n in nodes
        ],
        "sessions": [
            {
                "id": s.id,
                "candidate_id": s.candidate_id,
                "node_id": s.node_id,
                "status": s.status,
                "last_activity": s.last_activity,
            }
            for s in sessions
        ],
        "incidents": incident_rows,
        "audit_status": {
            "valid": audit_result.valid,
            "events_checked": audit_result.events_checked,
            "first_broken_sequence_no": audit_result.first_broken_sequence_no,
            "failure_reason": audit_result.failure_reason,
        },
        "latest_event_sequence_no": latest_sequence_no,
    }

