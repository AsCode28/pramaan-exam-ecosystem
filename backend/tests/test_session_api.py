"""Isolated API tests for Session + Answer + Heartbeat (Task 3).

Uses a temporary SQLite database per test session via FastAPI
dependency_overrides; the production pramaan.db is never touched.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.db import Base, Candidate, Event, Exam, Node, Question, Response, Session
from app.main import app
from app.services import event_ledger, session_service


@pytest.fixture()
def client(tmp_path):
    """TestClient wired to an isolated SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_api.db",
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
        # raise_server_exceptions=False so 500s surface as responses
        # (needed by the start-failure atomicity test).
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def seed(factory, question_exam_id=None):
    """One exam + node + candidate + question; returns ids dict."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title="E1", status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        node = Node(exam_id=exam.id, status="HEALTHY")
        candidate = Candidate(name="C1", roll_no="R-001")
        db.add_all([node, candidate])
        db.flush()
        q_exam = question_exam_id if question_exam_id is not None else exam.id
        question = Question(exam_id=q_exam, text="2+2?", marks=1)
        db.add(question)
        db.commit()
        return {
            "exam_id": exam.id,
            "node_id": node.id,
            "candidate_id": candidate.id,
            "question_id": question.id,
        }
    finally:
        db.close()


def start(client, ids):
    r = client.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": ids["node_id"],
        },
    )
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

def test_1_start_session_successfully(client):
    test_client, _ = client
    ids = seed(client[1])
    body = start(test_client, ids)
    assert body["status"] == "ACTIVE"
    assert body["exam_id"] == ids["exam_id"]
    assert body["candidate_id"] == ids["candidate_id"]
    assert body["node_id"] == ids["node_id"]
    assert body["session_id"] > 0
    assert body["event_sequence_no"] >= 1


def test_2_session_started_event_exists(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    db = factory()
    try:
        event = db.execute(
            select(Event).where(
                Event.session_id == body["session_id"],
                Event.event_type == "SESSION_STARTED",
            )
        ).scalar_one()
        assert event.sequence_no == body["event_sequence_no"]
        assert event.exam_id == ids["exam_id"]
    finally:
        db.close()


def test_3_start_with_invalid_refs_fails(client):
    test_client, factory = client
    ids = seed(factory)
    for field, bad in (
        ("exam_id", 9999),
        ("candidate_id", 9999),
        ("node_id", 9999),
    ):
        payload = {
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": ids["node_id"],
            field: bad,
        }
        r = test_client.post("/session/start", json=payload)
        assert r.status_code == 404, (field, r.text)
    # Node from another exam is rejected with 422.
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        other_exam = Exam(title="E2", status="ACTIVE", start_time=now, end_time=now)
        db.add(other_exam)
        db.flush()
        other_node = Node(exam_id=other_exam.id, status="HEALTHY")
        db.add(other_node)
        db.commit()
        other_node_id = other_node.id
    finally:
        db.close()
    r = test_client.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": other_node_id,
        },
    )
    assert r.status_code == 422, r.text


def test_4_answer_creates_answer_saved_event(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    r = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["appended"] is True
    assert count_events(factory, event_type="ANSWER_SAVED") == 1


def test_5_response_projection_created(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    db = factory()
    try:
        response = db.execute(
            select(Response).where(
                Response.session_id == body["session_id"],
                Response.question_id == ids["question_id"],
            )
        ).scalar_one()
        assert response.current_answer == "4"
    finally:
        db.close()

def test_6_response_last_event_id_points_to_event(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    ans = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    ).json()
    db = factory()
    try:
        response = db.execute(
            select(Response).where(
                Response.session_id == body["session_id"],
                Response.question_id == ids["question_id"],
            )
        ).scalar_one()
        assert response.last_event_id == ans["sequence_no"] == ans["last_event_id"]
        # last_event_id stores Event.sequence_no (no Event.id column exists).
        assert db.get(Event, ans["sequence_no"]).event_type == "ANSWER_SAVED"
    finally:
        db.close()


def test_7_second_answer_updates_same_response(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    first = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    ).json()
    second = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "four",
            "client_event_id": "a-2",
        },
    ).json()
    assert second["appended"] is True
    assert second["sequence_no"] > first["sequence_no"]
    db = factory()
    try:
        rows = db.execute(
            select(Response).where(
                Response.session_id == body["session_id"],
                Response.question_id == ids["question_id"],
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].current_answer == "four"
        assert rows[0].last_event_id == second["sequence_no"]
    finally:
        db.close()


def test_8_ordering_uses_sequence_no(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    now = datetime.now(timezone.utc)
    test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "late-client-time",
            "client_event_id": "a-1",
            "client_timestamp": (now + timedelta(hours=2)).isoformat(),
        },
    )
    test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "early-client-time",
            "client_event_id": "a-2",
            "client_timestamp": (now - timedelta(hours=2)).isoformat(),
        },
    )
    state = test_client.get(f"/session/{body['session_id']}/state").json()
    assert state["responses"][0]["current_answer"] == "early-client-time"


def test_9_duplicate_client_event_id_is_idempotent(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    first = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    ).json()
    retry = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    ).json()
    assert retry["appended"] is False
    assert retry["sequence_no"] == first["sequence_no"]


def test_10_duplicate_creates_exactly_one_event(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    for _ in range(3):
        test_client.post(
            f"/session/{body['session_id']}/answer",
            json={
                "question_id": ids["question_id"],
                "answer": "4",
                "client_event_id": "a-1",
            },
        )
    assert (
        count_events(factory, session_id=body["session_id"], client_event_id="a-1")
        == 1
    )

def test_11_state_returns_current_responses(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    state = test_client.get(f"/session/{body['session_id']}/state")
    assert state.status_code == 200, state.text
    responses = state.json()["responses"]
    assert len(responses) == 1
    assert responses[0]["question_id"] == ids["question_id"]
    assert responses[0]["current_answer"] == "4"


def test_12_state_returns_complete_client_event_id_set(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    for key in ("a-1", "a-2", "a-3"):
        test_client.post(
            f"/session/{body['session_id']}/answer",
            json={
                "question_id": ids["question_id"],
                "answer": key,
                "client_event_id": key,
            },
        )
    state = test_client.get(f"/session/{body['session_id']}/state").json()
    assert state["acknowledged_client_event_ids"] == ["a-1", "a-2", "a-3"]


def test_13_heartbeat_updates_activity_and_creates_event(client):
    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    before = body["last_activity"]
    r = test_client.post(
        f"/session/{body['session_id']}/heartbeat",
        json={"client_event_id": "hb-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["appended"] is True
    state = test_client.get(f"/session/{body['session_id']}/state").json()
    assert state["last_activity"] >= before
    assert count_events(factory, event_type="HEARTBEAT") == 1


def test_13b_duplicate_heartbeat_freezes_last_activity(client):
    """Same-key retry: no new event AND last_activity untouched; a fresh
    key advances last_activity again."""
    import time

    test_client, factory = client
    ids = seed(factory)
    body = start(test_client, ids)
    first = test_client.post(
        f"/session/{body['session_id']}/heartbeat",
        json={"client_event_id": "hb-1"},
    ).json()
    assert first["appended"] is True
    activity_after_first = test_client.get(
        f"/session/{body['session_id']}/state"
    ).json()["last_activity"]

    time.sleep(0.02)  # ensure backend clock can advance observably
    retry = test_client.post(
        f"/session/{body['session_id']}/heartbeat",
        json={"client_event_id": "hb-1"},
    ).json()
    assert retry["appended"] is False
    assert retry["sequence_no"] == first["sequence_no"]
    activity_after_retry = test_client.get(
        f"/session/{body['session_id']}/state"
    ).json()["last_activity"]
    assert activity_after_retry == activity_after_first
    assert count_events(factory, event_type="HEARTBEAT") == 1

    time.sleep(0.02)
    fresh = test_client.post(
        f"/session/{body['session_id']}/heartbeat",
        json={"client_event_id": "hb-2"},
    ).json()
    assert fresh["appended"] is True
    activity_after_fresh = test_client.get(
        f"/session/{body['session_id']}/state"
    ).json()["last_activity"]
    assert activity_after_fresh > activity_after_first
    assert count_events(factory, event_type="HEARTBEAT") == 2


def test_14_cross_exam_question_rejected(client):
    test_client, factory = client
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        other = Exam(title="OTHER", status="ACTIVE", start_time=now, end_time=now)
        db.add(other)
        db.flush()
        foreign_q = Question(exam_id=other.id, text="foreign?", marks=1)
        db.add(foreign_q)
        db.commit()
        foreign_q_id = foreign_q.id
    finally:
        db.close()
    ids = seed(factory)
    body = start(test_client, ids)
    r = test_client.post(
        f"/session/{body['session_id']}/answer",
        json={
            "question_id": foreign_q_id,
            "answer": "x",
            "client_event_id": "a-1",
        },
    )
    assert r.status_code == 422, r.text
    assert count_events(factory, event_type="ANSWER_SAVED") == 0


def test_15_invalid_session_rejected(client):
    test_client, factory = client
    ids = seed(factory)
    assert test_client.get("/session/99999/state").status_code == 404
    r = test_client.post(
        "/session/99999/answer",
        json={
            "question_id": ids["question_id"],
            "answer": "4",
            "client_event_id": "a-1",
        },
    )
    assert r.status_code == 404, r.text
    r = test_client.post("/session/99999/heartbeat", json={})
    assert r.status_code == 404, r.text


def test_16_start_failure_leaves_no_orphan_session(client, monkeypatch):
    test_client, factory = client
    ids = seed(factory)

    def boom(*args, **kwargs):
        raise RuntimeError("forced hash failure")

    monkeypatch.setattr(session_service.event_ledger, "compute_hash", boom)
    r = test_client.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_id"],
            "node_id": ids["node_id"],
        },
    )
    assert r.status_code == 500, r.text
    db = factory()
    try:
        active = db.execute(
            select(func.count())
            .select_from(Session)
            .where(Session.status == "ACTIVE")
        ).scalar_one()
        assert active == 0
        assert count_events(factory, event_type="SESSION_STARTED") == 0
    finally:
        db.close()
    # Service recovers once the fault is removed.
    monkeypatch.undo()
    body = start(test_client, ids)
    assert body["status"] == "ACTIVE"
