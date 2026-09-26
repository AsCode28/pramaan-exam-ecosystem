"""Tests for the tamper-evident audit verification service and API.

Verifies:
- valid chain (multi-exam, multi-session)
- modified payload
- modified hash
- broken previous_hash
- broken sequence relationship (sequence gap, out-of-order, first != 1)
- genesis mismatch
- empty ledger
- stops at the first broken event
- POST /audit/verify HTTP endpoint
- POST /demo/tamper HTTP endpoint (payload, hash, previous_hash on latest)
"""

import hashlib
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.core.hashing import genesis_hash
from app.db import Base, Candidate, Exam, Node, Session as ExamSession
from app.db.event import Event
from app.main import app
from app.services.audit_service import verify_ledger_chain
from app.services.event_ledger import append_event


@pytest.fixture()
def db(tmp_path):
    """Isolated fresh database session per test."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_audit.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def test_client(db):
    """FastAPI TestClient with overridden get_db."""
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def seed_events(db, count=5, exam_id=1):
    """Append count valid events to the ledger across exam/session."""
    results = []
    for i in range(1, count + 1):
        res = append_event(
            db,
            exam_id=exam_id,
            session_id=10,
            candidate_id=20,
            node_id=30,
            event_type="ANSWER_SELECTED",
            payload={"question_id": i, "answer": f"OPT-{i}"},
            client_event_id=f"evt-{i}",
        )
        results.append(res.event)
    return results


def test_empty_ledger_is_valid(db):
    report = verify_ledger_chain(db)
    assert report.valid is True
    assert report.events_checked == 0
    assert report.first_broken_sequence_no is None
    assert report.failure_reason is None


def test_valid_chain_verifies_completely(db):
    seed_events(db, count=4, exam_id=1)
    # Also append an event with exam_id=2 to check global chain across exams
    append_event(
        db,
        exam_id=2,
        session_id=11,
        candidate_id=21,
        node_id=31,
        event_type="SESSION_STARTED",
        payload={"mode": "exam"},
        client_event_id="evt-other-exam",
    )

    report = verify_ledger_chain(db)
    assert report.valid is True
    assert report.events_checked == 5
    assert report.first_broken_sequence_no is None
    assert report.failure_reason is None


def test_modified_payload_fails_verification(db):
    events = seed_events(db, count=4)
    event_2 = events[1]
    assert event_2.sequence_no == 2

    # Directly mutate event 2's payload in DB
    event_2.payload = {"question_id": 2, "answer": "TAMPERED"}
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 2
    assert report.first_broken_sequence_no == 2
    assert report.failure_reason is not None
    assert "Hash mismatch at sequence 2" in report.failure_reason


def test_modified_hash_fails_verification(db):
    events = seed_events(db, count=4)
    event_3 = events[2]
    assert event_3.sequence_no == 3

    # Directly mutate event 3's hash in DB (flip last char guaranteed to change)
    orig_hash = event_3.hash
    flip = "1" if orig_hash.endswith("0") else "0"
    event_3.hash = orig_hash[:-1] + flip
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 3
    assert report.first_broken_sequence_no == 3
    assert report.failure_reason is not None
    assert "Hash mismatch at sequence 3" in report.failure_reason


def test_broken_previous_hash_link_fails_verification(db):
    events = seed_events(db, count=4)
    event_2 = events[1]
    assert event_2.sequence_no == 2

    # Mutate event 2's previous_hash
    event_2.previous_hash = "f" * 64
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 2
    assert report.first_broken_sequence_no == 2
    assert report.failure_reason is not None
    assert "Broken previous_hash link at sequence 2" in report.failure_reason


def test_broken_sequence_relationship_gap_fails_verification(db):
    events = seed_events(db, count=3)
    # Events are 1, 2, 3. Modify event 3 sequence_no to 5.
    event_3 = events[2]
    event_3.sequence_no = 5
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 3
    assert report.first_broken_sequence_no == 5
    assert report.failure_reason is not None
    assert "Broken sequence relationship at sequence 5" in report.failure_reason


def test_first_event_sequence_no_must_be_one(db):
    # Only 1 event seeded, then shift its sequence_no to 2 (so no unique clash)
    events = seed_events(db, count=1)
    event_1 = events[0]
    event_1.sequence_no = 2
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 1
    assert report.first_broken_sequence_no == 2
    assert report.failure_reason is not None
    assert "first event must have sequence_no == 1" in report.failure_reason


def test_genesis_mismatch_fails_verification(db):
    events = seed_events(db, count=3, exam_id=9)
    event_1 = events[0]
    assert event_1.sequence_no == 1
    # Corrupt event 1's previous_hash so it does not match genesis_hash(9)
    event_1.previous_hash = genesis_hash(888)  # different exam genesis
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    assert report.events_checked == 1
    assert report.first_broken_sequence_no == 1
    assert report.failure_reason is not None
    assert "Genesis mismatch at sequence 1" in report.failure_reason


def test_stops_at_first_broken_event(db):
    events = seed_events(db, count=5)
    # Corrupt both event 2 and event 4
    events[1].payload = {"tampered": 2}
    events[3].payload = {"tampered": 4}
    db.commit()

    report = verify_ledger_chain(db)
    assert report.valid is False
    # Inspected events 1 and 2, stopped at 2. events 3, 4, 5 are NOT inspected.
    assert report.events_checked == 2
    assert report.first_broken_sequence_no == 2
    assert "sequence 2" in report.failure_reason


def test_audit_verify_api_empty_ledger(test_client):
    response = test_client.post("/audit/verify")
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["events_checked"] == 0
    assert data["first_broken_sequence_no"] is None
    assert data["failure_reason"] is None


def test_audit_verify_api_valid_chain(test_client, db):
    seed_events(db, count=3)
    response = test_client.post("/audit/verify")
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["events_checked"] == 3
    assert data["first_broken_sequence_no"] is None
    assert data["failure_reason"] is None


def test_demo_tamper_api_payload_and_verify(test_client, db):
    seed_events(db, count=3)
    # Tamper latest event payload via POST /demo/tamper
    tamper_res = test_client.post("/demo/tamper", json={"target": "latest", "field": "payload"})
    assert tamper_res.status_code == 200
    tamper_data = tamper_res.json()
    assert tamper_data["tampered_sequence_no"] == 3
    assert tamper_data["field"] == "payload"

    # Audit verify should now report failure at sequence 3
    verify_res = test_client.post("/audit/verify")
    assert verify_res.status_code == 200
    report = verify_res.json()
    assert report["valid"] is False
    assert report["events_checked"] == 3
    assert report["first_broken_sequence_no"] == 3


def test_demo_tamper_api_hash(test_client, db):
    seed_events(db, count=2)
    tamper_res = test_client.post("/demo/tamper", json={"target": "latest", "field": "hash"})
    assert tamper_res.status_code == 200
    assert tamper_res.json()["tampered_sequence_no"] == 2

    verify_res = test_client.post("/audit/verify")
    report = verify_res.json()
    assert report["valid"] is False
    assert report["first_broken_sequence_no"] == 2


def test_demo_tamper_api_previous_hash(test_client, db):
    seed_events(db, count=2)
    tamper_res = test_client.post("/demo/tamper", json={"target": "latest", "field": "previous_hash"})
    assert tamper_res.status_code == 200
    assert tamper_res.json()["tampered_sequence_no"] == 2

    verify_res = test_client.post("/audit/verify")
    report = verify_res.json()
    assert report["valid"] is False
    assert report["first_broken_sequence_no"] == 2


def test_demo_tamper_rejects_arbitrary_target_and_invalid_field(test_client, db):
    seed_events(db, count=2)
    # Target other than latest must be rejected
    r1 = test_client.post("/demo/tamper", json={"target": "1", "field": "payload"})
    assert r1.status_code == 400

    # Invalid field must be rejected
    r2 = test_client.post("/demo/tamper", json={"target": "latest", "field": "nonexistent"})
    assert r2.status_code == 400


def test_demo_tamper_on_empty_ledger_returns_404(test_client):
    res = test_client.post("/demo/tamper", json={"target": "latest", "field": "payload"})
    assert res.status_code == 404
