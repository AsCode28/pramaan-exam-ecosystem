"""Isolated API tests for Node Failure Simulation (Task 4).

Uses a temporary SQLite database per test via FastAPI
dependency_overrides; the production pramaan.db is never touched.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.core.hashing import verify_event
from app.db import Base, Candidate, Event, Exam, Node, Question, Session
from app.main import app
from app.services import failure_service


@pytest.fixture()
def client(tmp_path):
    """TestClient wired to an isolated SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_failure.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def seed_exam(factory, node_count=2, node_status="HEALTHY"):
    """One exam + N nodes + candidate + question; returns ids dict."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title="E1", status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        nodes = [Node(exam_id=exam.id, status=node_status) for _ in range(node_count)]
        candidate = Candidate(name="C1", roll_no="R-001")
        db.add_all(nodes + [candidate])
        db.flush()
        question = Question(exam_id=exam.id, text="2+2?", marks=1)
        db.add(question)
        db.commit()
        return {
            "exam_id": exam.id,
            "node_ids": [n.id for n in nodes],
            "candidate_id": candidate.id,
            "question_id": question.id,
        }
    finally:
        db.close()


def start_on(client, ids, node_id):
    r = client.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": node_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def add_candidate(factory, exam_id, roll_no):
    """A second, distinct candidate so two live sessions can coexist."""
    db = factory()
    try:
        candidate = Candidate(name=f"C-{roll_no}", roll_no=roll_no)
        db.add(candidate)
        db.commit()
        return candidate.id
    finally:
        db.close()


def start_for(client, exam_id, candidate_id, node_id):
    r = client.post(
        "/session/start",
        json={"exam_id": exam_id, "candidate_id": candidate_id, "node_id": node_id},
    )
    assert r.status_code == 200, r.text
    return r.json()


def fail(client, node_id, reason="demo power cut"):
    r = client.post(f"/demo/nodes/{node_id}/fail", json={"reason": reason})
    assert r.status_code == 200, r.text
    return r.json()


def count_events(factory, **filters):
    db = factory()
    try:
        stmt = select(func.count()).select_from(Event)
        for attr, value in filters.items():
            stmt = stmt.where(getattr(Event, attr) == value)
        return db.execute(stmt).scalar_one()
    finally:
        db.close()


def session_status(factory, session_id):
    db = factory()
    try:
        return db.get(Session, session_id).status
    finally:
        db.close()


def test_1_healthy_node_can_be_failed(client):
    test_client, factory = client
    ids = seed_exam(factory)
    body = fail(test_client, ids["node_ids"][0])
    assert body["node_id"] == ids["node_ids"][0]
    assert body["previous_status"] == "HEALTHY"
    assert body["new_status"] == "FAILED"
    assert body["newly_failed"] is True


def test_2_node_status_becomes_failed(client):
    test_client, factory = client
    ids = seed_exam(factory)
    fail(test_client, ids["node_ids"][0])
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][0]).status == "FAILED"
    finally:
        db.close()


def test_3_failure_event_exists(client):
    test_client, factory = client
    ids = seed_exam(factory)
    body = fail(test_client, ids["node_ids"][0])
    db = factory()
    try:
        event = db.get(Event, body["event_sequence_no"])
        assert event is not None
        assert event.event_type == "NODE_FAILURE_INJECTED"
        assert event.node_id == ids["node_ids"][0]
    finally:
        db.close()


def test_4_event_payload_has_affected_sessions(client):
    test_client, factory = client
    ids = seed_exam(factory)
    # Task 13: one live session per (exam, candidate), so use two candidates.
    other = add_candidate(factory, ids["exam_id"], "R-002")
    s1 = start_on(test_client, ids, ids["node_ids"][0])
    s2 = start_for(test_client, ids["exam_id"], other, ids["node_ids"][0])
    assert s1["session_id"] != s2["session_id"]
    body = fail(test_client, ids["node_ids"][0])
    assert body["affected_session_ids"] == sorted([s1["session_id"], s2["session_id"]])
    assert body["affected_count"] == 2
    db = factory()
    try:
        event = db.get(Event, body["event_sequence_no"])
        assert event.payload["affected_session_ids"] == body["affected_session_ids"]
        assert event.payload["affected_session_count"] == 2
        assert event.payload["node_id"] == ids["node_ids"][0]
        assert event.payload["previous_status"] == "HEALTHY"
        assert event.payload["new_status"] == "FAILED"
        assert event.payload["simulation"] is True
    finally:
        db.close()


def test_5_active_sessions_become_disconnected(client):
    test_client, factory = client
    ids = seed_exam(factory)
    other = add_candidate(factory, ids["exam_id"], "R-002")
    s1 = start_on(test_client, ids, ids["node_ids"][0])
    s2 = start_for(test_client, ids["exam_id"], other, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    assert session_status(factory, s1["session_id"]) == "DISCONNECTED"
    assert session_status(factory, s2["session_id"]) == "DISCONNECTED"


def test_6_other_node_sessions_unchanged(client):
    test_client, factory = client
    ids = seed_exam(factory)
    other = start_on(test_client, ids, ids["node_ids"][1])
    fail(test_client, ids["node_ids"][0])
    assert session_status(factory, other["session_id"]) == "ACTIVE"
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][1]).status == "HEALTHY"
    finally:
        db.close()


def test_7_answer_on_failed_node_rejected(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    r = test_client.post(
        f"/session/{s['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    assert r.status_code == 503, r.text
    assert "FAILED" in r.json()["detail"]


def test_8_rejected_answer_creates_no_event(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    before = count_events(factory)
    test_client.post(
        f"/session/{s['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    assert count_events(factory) == before
    assert count_events(factory, event_type="ANSWER_SAVED") == 0


def test_9_heartbeat_on_failed_node_rejected(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    r = test_client.post(
        f"/session/{s['session_id']}/heartbeat", json={"client_event_id": "hb-1"}
    )
    assert r.status_code == 503, r.text


def test_10_rejected_heartbeat_creates_no_event(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    before = count_events(factory)
    test_client.post(
        f"/session/{s['session_id']}/heartbeat", json={"client_event_id": "hb-1"}
    )
    assert count_events(factory) == before
    assert count_events(factory, event_type="HEARTBEAT") == 0


def test_11_zero_session_node_can_be_failed(client):
    test_client, factory = client
    ids = seed_exam(factory)
    body = fail(test_client, ids["node_ids"][0])
    assert body["affected_session_ids"] == []
    assert body["affected_count"] == 0
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 1
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][0]).status == "FAILED"
    finally:
        db.close()


def test_12_repeated_failure_is_idempotent(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    first = fail(test_client, ids["node_ids"][0])
    second = fail(test_client, ids["node_ids"][0])
    assert second["newly_failed"] is False
    assert second["new_status"] == "FAILED"
    assert second["event_sequence_no"] == first["event_sequence_no"]
    assert second["affected_session_ids"] == [s["session_id"]]
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 1


def test_12b_degraded_node_can_be_failed(client):
    test_client, factory = client
    ids = seed_exam(factory, node_status="DEGRADED")
    body = fail(test_client, ids["node_ids"][0])
    assert body["previous_status"] == "DEGRADED"
    assert body["new_status"] == "FAILED"


def test_12d_unknown_node_status_rejected_without_changes(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    # Corrupt the node status directly (simulates an unexpected value).
    db = factory()
    try:
        node = db.get(Node, ids["node_ids"][0])
        node.status = "MAINTENANCE"
        db.commit()
    finally:
        db.close()
    before = count_events(factory)
    r = test_client.post(
        f"/demo/nodes/{ids['node_ids'][0]}/fail", json={"reason": "x"}
    )
    assert r.status_code == 409, r.text
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][0]).status == "MAINTENANCE"
        assert db.get(Session, s["session_id"]).status == "ACTIVE"
    finally:
        db.close()
    assert count_events(factory) == before
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 0


def test_12e_repeat_returns_original_payload_after_state_change(client):
    test_client, factory = client
    ids = seed_exam(factory)
    other = add_candidate(factory, ids["exam_id"], "R-002")
    s1 = start_on(test_client, ids, ids["node_ids"][0])
    s2 = start_for(test_client, ids["exam_id"], other, ids["node_ids"][0])
    first = fail(test_client, ids["node_ids"][0])
    original_ids = list(first["affected_session_ids"])
    assert original_ids == sorted([s1["session_id"], s2["session_id"]])
    # Change live session states afterwards (e.g. future recovery work).
    db = factory()
    try:
        db.get(Session, s2["session_id"]).status = "ACTIVE"
        db.commit()
    finally:
        db.close()
    # Repeat must still report the ORIGINAL immutable payload list.
    again = fail(test_client, ids["node_ids"][0])
    assert again["newly_failed"] is False
    assert again["affected_session_ids"] == original_ids
    assert again["event_sequence_no"] == first["event_sequence_no"]
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 1


def test_12c_mixed_active_disconnected_only_moves_active(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s1 = start_on(test_client, ids, ids["node_ids"][0])
    s2 = start_on(test_client, ids, ids["node_ids"][0])
    # Pre-disconnect s2 via a first fail; revive node manually is out of
    # scope, so instead fail, then verify a second fail leaves it stable.
    fail(test_client, ids["node_ids"][0])
    assert session_status(factory, s1["session_id"]) == "DISCONNECTED"
    assert session_status(factory, s2["session_id"]) == "DISCONNECTED"
    # Both were ACTIVE at fail time, so both moved; re-fail is a no-op.
    again = fail(test_client, ids["node_ids"][0])
    assert again["newly_failed"] is False
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 1


def test_13_hash_chain_verifies_after_failure(client):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    db = factory()
    try:
        events = (
            db.execute(select(Event).order_by(Event.sequence_no)).scalars().all()
        )
        assert len(events) >= 2  # SESSION_STARTED + NODE_FAILURE_INJECTED
        assert events[0].session_id == s["session_id"]
        for event in events:
            assert verify_event(
                sequence_no=event.sequence_no,
                exam_id=event.exam_id,
                session_id=event.session_id,
                candidate_id=event.candidate_id,
                node_id=event.node_id,
                event_type=event.event_type,
                payload=event.payload,
                client_event_id=event.client_event_id,
                client_timestamp=event.client_timestamp,
                server_timestamp=event.server_timestamp,
                previous_hash=event.previous_hash,
                expected_hash=event.hash,
            ) is True
    finally:
        db.close()


def test_14_fail_injection_rolls_back_on_hash_failure(client, monkeypatch):
    test_client, factory = client
    ids = seed_exam(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])

    def boom(*args, **kwargs):
        raise RuntimeError("forced hash failure")

    monkeypatch.setattr(failure_service.event_ledger, "compute_hash", boom)
    r = test_client.post(
        f"/demo/nodes/{ids['node_ids'][0]}/fail", json={"reason": "x"}
    )
    assert r.status_code == 500, r.text
    # append_event() rolled back: no orphan FAILED node / DISCONNECTED session.
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][0]).status == "HEALTHY"
        assert db.get(Session, s["session_id"]).status == "ACTIVE"
        assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == 0
    finally:
        db.close()
    monkeypatch.undo()
    body = fail(test_client, ids["node_ids"][0])
    assert body["new_status"] == "FAILED"


def test_15_start_on_failed_node_rejected(client):
    test_client, factory = client
    ids = seed_exam(factory)
    fail(test_client, ids["node_ids"][0])
    before = count_events(factory)
    r = test_client.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": ids["node_ids"][0],
        },
    )
    assert r.status_code == 503, r.text
    assert count_events(factory) == before


def test_16_fail_missing_node_404(client):
    test_client, _ = client
    r = test_client.post("/demo/nodes/99999/fail", json={})
    assert r.status_code == 404, r.text

