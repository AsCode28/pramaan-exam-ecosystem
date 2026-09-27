"""Dev-only database verification for the PRAMAAN screening prototype.

Exercises the full relationship chain, the Response unique constraint,
Event idempotency, and sequence_no ordering. Runs in a transaction that is
always rolled back, so the dev database is left untouched.

Usage (from backend/):
    python scripts/verify_db.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.db import (  # noqa: E402
    Candidate,
    Event,
    Exam,
    Incident,
    IncidentSession,
    Node,
    Question,
    Response,
    Session,
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        exam = Exam(
            title="Screening Test",
            status="DRAFT",
            start_time=utcnow(),
            end_time=utcnow(),
        )
        node = Node(exam=exam, status="HEALTHY")
        candidate = Candidate(name="Test Candidate", roll_no="TEST-001")
        db.add_all([exam, node, candidate])
        db.flush()

        session = Session(
            exam_id=exam.id,
            candidate_id=candidate.id,
            node_id=node.id,  # FK to nodes.id, not free-form
            status="ACTIVE",
            started_at=utcnow(),
            last_activity=utcnow(),
        )
        question = Question(exam_id=exam.id, text="2 + 2 = ?", marks=1)
        db.add_all([session, question])
        db.flush()

        # Response projection starts with last_event_id NULL.
        response = Response(
            session_id=session.id,
            question_id=question.id,
            current_answer="4",
        )
        db.add(response)
        db.flush()
        assert response.last_event_id is None, "last_event_id should default to NULL"

        # Duplicate (session_id, question_id) must be rejected.
        # SAVEPOINT keeps the outer test data intact after the rollback.
        try:
            with db.begin_nested():
                db.add(
                    Response(
                        session_id=session.id,
                        question_id=question.id,
                        current_answer="four",
                    )
                )
                db.flush()
        except IntegrityError:
            print("OK: duplicate Response(session_id, question_id) rejected")
        else:
            raise AssertionError("Response unique constraint NOT enforced")

        # Event ledger: append two events, verify sequence ordering.
        e1 = Event(
            client_event_id="evt-1",
            session_id=session.id,
            candidate_id=candidate.id,
            node_id=node.id,
            event_type="ANSWER_SELECTED",
            payload={"question_id": question.id, "answer": "4"},
            client_timestamp=utcnow(),
        )
        e2 = Event(
            client_event_id="evt-2",
            session_id=session.id,
            candidate_id=candidate.id,
            node_id=node.id,
            event_type="ANSWER_SELECTED",
            payload={"question_id": question.id, "answer": "4"},
            client_timestamp=utcnow(),
        )
        db.add_all([e1, e2])
        db.flush()
        assert e2.sequence_no > e1.sequence_no, "sequence_no must order events"
        print(f"OK: event ordering e1={e1.sequence_no} < e2={e2.sequence_no}")

        # Idempotency: replay of (session_id, client_event_id) rejected.
        # SAVEPOINT keeps the outer test data intact after the rollback.
        try:
            with db.begin_nested():
                db.add(
                    Event(
                        client_event_id="evt-1",
                        session_id=session.id,
                        candidate_id=candidate.id,
                        node_id=node.id,
                        event_type="ANSWER_SELECTED",
                        payload={},
                    )
                )
                db.flush()
        except IntegrityError:
            print("OK: duplicate Event(session_id, client_event_id) rejected")
        else:
            raise AssertionError("Event idempotency constraint NOT enforced")

        incident = Incident(
            exam_id=exam.id,
            severity="HIGH",
            status="OPEN",
            created_from_event_id=e2.sequence_no,
            detected_at=utcnow(),
        )
        db.add(incident)
        db.flush()
        db.add(IncidentSession(incident_id=incident.id, session_id=session.id))
        db.flush()

        # Expire everything and re-read through relationships.
        db.expire_all()
        got = db.get(Incident, incident.id)
        assert got.origin_event.sequence_no == e2.sequence_no
        assert got.session_links[0].session.candidate.roll_no == "TEST-001"
        assert got.exam.questions[0].responses[0].current_answer == "4"
        print("OK: relationship chain Incident->Event/Session->Candidate/Response")

        print("ALL VERIFICATION CHECKS PASSED")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
