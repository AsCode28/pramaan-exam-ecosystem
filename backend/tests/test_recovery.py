"""Isolated API tests for Node Recovery + Reconciliation (Task 5).

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
from app.db import Base, Candidate, Event, Exam, Node, Question, Response, Session
from app.main import app
from app.services import recovery_service
from app.services.event_ledger import AppendResult


@pytest.fixture()
def client(tmp_path):
    """TestClient wired to an isolated SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_recovery.db",
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


def seed(factory, node_count=1, questions=2):
    """One exam + nodes + candidate + questions; returns ids dict."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title="E1", status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        nodes = [Node(exam_id=exam.id, status="HEALTHY") for _ in range(node_count)]
        candidate = Candidate(name="C1", roll_no="R-001")
        db.add_all(nodes + [candidate])
        db.flush()
        qs = [Question(exam_id=exam.id, text=f"Q{i}?", marks=1) for i in range(questions)]
        db.add_all(qs)
        db.commit()
        return {
            "exam_id": exam.id,
            "node_ids": [n.id for n in nodes],
            "candidate_id": candidate.id,
            "question_ids": [q.id for q in qs],
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


def answer(client, session_id, qid, ans, key):
    r = client.post(
        f"/session/{session_id}/answer",
        json={"question_id": qid, "answer": ans, "client_event_id": key},
    )
    assert r.status_code == 200, r.text
    return r.json()


def fail(client, node_id):
    r = client.post(f"/demo/nodes/{node_id}/fail", json={"reason": "demo"})
    assert r.status_code == 200, r.text
    return r.json()


def recover(client, node_id):
    r = client.post(f"/demo/nodes/{node_id}/recover", json={"reason": "demo"})
    assert r.status_code == 200, r.text
    return r.json()


def reconcile(client, session_id, events):
    r = client.post(f"/session/{session_id}/reconcile", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def buf(key, qid, ans):
    return {"client_event_id": key, "question_id": qid, "answer": ans}


def count_events(factory, **filters):
    db = factory()
    try:
        stmt = select(func.count()).select_from(Event)
        for attr, value in filters.items():
            stmt = stmt.where(getattr(Event, attr) == value)
        return db.execute(stmt).scalar_one()
    finally:
        db.close()


def response_row(factory, session_id, qid):
    db = factory()
    try:
        return db.execute(
            select(Response).where(
                Response.session_id == session_id, Response.question_id == qid
            )
        ).scalar_one_or_none()
    finally:
        db.close()


def test_r1_failed_node_can_recover(client):
    test_client, factory = client
    ids = seed(factory)
    fail(test_client, ids["node_ids"][0])
    body = recover(test_client, ids["node_ids"][0])
    assert body["node_id"] == ids["node_ids"][0]
    assert body["previous_status"] == "FAILED"
    assert body["new_status"] == "HEALTHY"
    assert body["newly_recovered"] is True


def test_r2_node_becomes_healthy(client):
    test_client, factory = client
    ids = seed(factory)
    fail(test_client, ids["node_ids"][0])
    recover(test_client, ids["node_ids"][0])
    db = factory()
    try:
        assert db.get(Node, ids["node_ids"][0]).status == "HEALTHY"
    finally:
        db.close()


def test_r3_disconnected_become_recovering(client):
    test_client, factory = client
    ids = seed(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    body = recover(test_client, ids["node_ids"][0])
    assert body["affected_session_ids"] == [s["session_id"]]
    db = factory()
    try:
        assert db.get(Session, s["session_id"]).status == "RECOVERING"
    finally:
        db.close()


def test_r4_not_immediately_active(client):
    test_client, factory = client
    ids = seed(factory)
    s = start_on(test_client, ids, ids["node_ids"][0])
    fail(test_client, ids["node_ids"][0])
    recover(test_client, ids["node_ids"][0])
    state = test_client.get(f"/session/{s['session_id']}/state").json()
    assert state["status"] == "RECOVERING"


def test_r5_recovery_event_exists(client):
    test_client, factory = client
    ids = seed(factory)
    body = recover(test_client, fail(test_client, ids["node_ids"][0])["node_id"])
    db = factory()
    try:
        event = db.get(Event, body["event_sequence_no"])
        assert event.event_type == "NODE_RECOVERY_INITIATED"
        assert event.payload["simulation"] is True
        assert event.payload["new_status"] == "HEALTHY"
    finally:
        db.close()


def test_r6_repeat_recover_idempotent(client):
    test_client, factory = client
    ids = seed(factory)
    fail(test_client, ids["node_ids"][0])
    first = recover(test_client, ids["node_ids"][0])
    second = recover(test_client, ids["node_ids"][0])
    assert second["newly_recovered"] is False
    assert second["event_sequence_no"] == first["event_sequence_no"]
    assert count_events(factory, event_type="NODE_RECOVERY_INITIATED") == 1


def test_r7_invalid_recovery_state_rejected(client):
    test_client, factory = client
    ids = seed(factory)
    r = test_client.post(f"/demo/nodes/{ids['node_ids'][0]}/recover", json={})
    # HEALTHY node with no recovery event on record: defensive 404 path is
    # unreachable via API seed (no prior recovery), so instead assert the
    # DEGRADED source state is rejected with 409.
    assert r.status_code in (404, 409), r.text
    db = factory()
    try:
        db.get(Node, ids["node_ids"][0]).status = "DEGRADED"
        db.commit()
    finally:
        db.close()
    r = test_client.post(f"/demo/nodes/{ids['node_ids'][0]}/recover", json={})
    assert r.status_code == 409, r.text
    assert count_events(factory, event_type="NODE_RECOVERY_INITIATED") == 0
    r = test_client.post("/demo/nodes/99999/recover", json={})
    assert r.status_code == 404, r.text


def _to_recovering(test_client, factory, ids, with_answer=True):
    s = start_on(test_client, ids, ids["node_ids"][0])
    if with_answer:
        answer(test_client, s["session_id"], ids["question_ids"][0], "A", "pre-1")
    fail(test_client, ids["node_ids"][0])
    recover(test_client, ids["node_ids"][0])
    return s


def test_r8_recovering_accepts_reconcile(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [buf("b-1", ids["question_ids"][0], "A")])
    assert report["reconciliation_complete"] is True
    assert report["newly_reconciled_client_event_ids"] == ["b-1"]
    assert report["status"] == "ACTIVE"


def test_r9_answer_blocked_in_recovering(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    before = count_events(factory)
    r = test_client.post(
        f"/session/{s['session_id']}/answer",
        json={"question_id": ids["question_ids"][0], "answer": "A", "client_event_id": "x-1"},
    )
    assert r.status_code == 409, r.text
    assert count_events(factory) == before


def test_r10_new_buffered_event_appended_once(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    reconcile(test_client, s["session_id"], [buf("b-1", ids["question_ids"][0], "A")])
    assert count_events(factory, session_id=s["session_id"], client_event_id="b-1") == 1


def test_r11_identical_retry_already_acknowledged(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    # Answered BEFORE the failure, so reconciliation must recognise it.
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    report = reconcile(test_client, s["session_id"], [buf("pre-1", q, "A")])
    assert report["already_acknowledged_client_event_ids"] == ["pre-1"]
    assert report["newly_reconciled_client_event_ids"] == []
    assert count_events(factory, session_id=s["session_id"], client_event_id="pre-1") == 1


def test_r12_conflicting_retry_is_mismatch(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    report = reconcile(test_client, s["session_id"], [buf("pre-1", q, "TAMPERED")])
    assert report["mismatched_client_event_ids"] == ["pre-1"]
    assert report["reconciliation_complete"] is False
    assert report["status"] == "RECOVERING"


def test_r13_mismatch_does_not_overwrite_projection(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    before = response_row(factory, s["session_id"], q)
    reconcile(test_client, s["session_id"], [buf("pre-1", q, "TAMPERED")])
    after = response_row(factory, s["session_id"], q)
    assert after.current_answer == "A"
    assert after.last_event_id == before.last_event_id


def test_r14_pre_failure_accept_recognized(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    report = reconcile(
        test_client, s["session_id"], [buf("pre-1", q, "A"), buf("b-1", q, "B")]
    )
    assert "pre-1" in report["already_acknowledged_client_event_ids"]
    assert report["newly_reconciled_client_event_ids"] == ["b-1"]
    assert report["reconciliation_complete"] is True
    assert report["status"] == "ACTIVE"


def test_r15_multiple_buffered_events_reconcile(client):
    test_client, factory = client
    ids = seed(factory)
    q0, q1 = ids["question_ids"]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(
        test_client, s["session_id"], [buf("b-1", q0, "A"), buf("b-2", q1, "B")]
    )
    assert sorted(report["newly_reconciled_client_event_ids"]) == ["b-1", "b-2"]
    assert report["reconciliation_complete"] is True


def test_r16_ordering_follows_sequence_no(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    events = [
        {"client_event_id": "b-1", "question_id": q, "answer": "A",
         "client_timestamp": "2030-01-01T00:00:00"},
        {"client_event_id": "b-2", "question_id": q, "answer": "B",
         "client_timestamp": "1990-01-01T00:00:00"},
    ]
    report = reconcile(test_client, s["session_id"], events)
    assert report["newly_reconciled_client_event_ids"] == ["b-1", "b-2"]
    assert report["acknowledged_client_event_ids"][-2:] == ["b-1", "b-2"]
    assert response_row(factory, s["session_id"], q).current_answer == "B"


def test_r17_exact_acknowledged_set_returned(client):
    test_client, factory = client
    ids = seed(factory)
    q0, q1 = ids["question_ids"]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    report = reconcile(test_client, s["session_id"], [buf("b-1", q1, "B")])
    assert report["acknowledged_client_event_ids"] == ["pre-1", "b-1"]


def test_r18_missing_ids_reported(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [])
    assert report["submitted_client_event_ids"] == []
    assert report["missing_client_event_ids"] == []
    assert report["reconciliation_complete"] is False
    assert report["status"] == "RECOVERING"


def test_r19_mismatch_does_not_activate(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    reconcile(
        test_client, s["session_id"], [buf("pre-1", q, "TAMPERED"), buf("b-1", q, "Z")]
    )
    db = factory()
    try:
        assert db.get(Session, s["session_id"]).status == "RECOVERING"
    finally:
        db.close()
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0


def test_r20_successful_reconciliation_activates(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [buf("b-1", ids["question_ids"][0], "A")])
    assert report["status"] == "ACTIVE"
    assert report["reconciliation_complete"] is True
    state = test_client.get(f"/session/{s['session_id']}/state").json()
    assert state["status"] == "ACTIVE"
    # And normal answers work again.
    r = test_client.post(
        f"/session/{s['session_id']}/answer",
        json={"question_id": ids["question_ids"][0], "answer": "C", "client_event_id": "post-1"},
    )
    assert r.status_code == 200, r.text


def test_r21_session_recovered_event_exists(client):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [buf("b-1", ids["question_ids"][0], "A")])
    assert report["recovered_event_sequence_no"] is not None
    db = factory()
    try:
        event = db.get(Event, report["recovered_event_sequence_no"])
        assert event.event_type == "SESSION_RECOVERED"
        assert event.payload["resulting_status"] == "ACTIVE"
        assert event.payload["reconciled_event_ids"] == ["b-1"]
        assert event.payload["mismatch_count"] == 0
    finally:
        db.close()


def test_r22_projection_points_to_authoritative_sequence(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [buf("b-1", q, "A")])
    row = response_row(factory, s["session_id"], q)
    assert row.last_event_id == report["recovered_event_sequence_no"] - 1
    db = factory()
    try:
        ev = db.get(Event, row.last_event_id)
        assert ev.client_event_id == "b-1"
        assert ev.payload["answer"] == "A"
    finally:
        db.close()


def test_r23_duplicate_ids_in_one_request_deterministic(client):
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(
        test_client, s["session_id"], [buf("dup-1", q, "A"), buf("dup-1", q, "A")]
    )
    assert report["submitted_client_event_ids"] == ["dup-1"]
    assert count_events(factory, session_id=s["session_id"], client_event_id="dup-1") == 1
    assert report["reconciliation_complete"] is True


def test_r24_hash_failure_leaves_correct_state(client, monkeypatch):
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    def boom(*args, **kwargs):
        raise RuntimeError("forced hash failure")

    monkeypatch.setattr(recovery_service.event_ledger, "compute_hash", boom)
    report = reconcile(test_client, s["session_id"], [buf("b-1", ids["question_ids"][0], "A")])
    assert report["missing_client_event_ids"] == ["b-1"]
    assert report["rejected_reasons"]["b-1"] == "ledger_append_failed"
    assert report["reconciliation_complete"] is False
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0
    db = factory()
    try:
        assert db.get(Session, s["session_id"]).status == "RECOVERING"
    finally:
        db.close()


def test_r5a_projection_failure_then_retry_repairs_before_active(client, monkeypatch):
    """R5-A: event commits, projection fails; retry heals, only then ACTIVE."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    def boom(*args, **kwargs):
        raise RuntimeError("forced projection failure")

    monkeypatch.setattr(recovery_service, "apply_answer_projection", boom)
    first = reconcile(test_client, s["session_id"], [buf("b-1", q, "A")])
    assert first["missing_client_event_ids"] == ["b-1"]
    assert first["rejected_reasons"]["b-1"] == "projection_failed"
    assert first["reconciliation_complete"] is False
    assert first["status"] == "RECOVERING"
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0
    # The event itself is authoritative and already durable.
    assert count_events(factory, session_id=s["session_id"], client_event_id="b-1") == 1
    assert response_row(factory, s["session_id"], q) is None

    monkeypatch.undo()
    second = reconcile(test_client, s["session_id"], [buf("b-1", q, "A")])
    assert second["already_acknowledged_client_event_ids"] == ["b-1"]
    assert second["reconciliation_complete"] is True
    assert second["status"] == "ACTIVE"
    assert count_events(factory, session_id=s["session_id"], client_event_id="b-1") == 1
    row = response_row(factory, s["session_id"], q)
    assert row.current_answer == "A" and row.last_event_id is not None


def test_r5b_identical_event_repairs_stale_projection(client):
    """R5-B: existing identical event with lost Response is repaired."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=True)
    # Simulate a lost projection from before the failure.
    db = factory()
    try:
        row = db.execute(
            select(Response).where(
                Response.session_id == s["session_id"], Response.question_id == q
            )
        ).scalar_one()
        db.delete(row)
        db.commit()
    finally:
        db.close()
    assert response_row(factory, s["session_id"], q) is None
    report = reconcile(test_client, s["session_id"], [buf("pre-1", q, "A")])
    assert report["already_acknowledged_client_event_ids"] == ["pre-1"]
    assert report["reconciliation_complete"] is True
    assert report["status"] == "ACTIVE"
    row = response_row(factory, s["session_id"], q)
    assert row.current_answer == "A"


def test_r5c_projection_failure_blocks_session_recovered(client, monkeypatch):
    """R5-C: one poisoned item must not produce SESSION_RECOVERED."""
    test_client, factory = client
    ids = seed(factory)
    q0, q1 = ids["question_ids"]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    real = recovery_service.apply_answer_projection

    def flaky(db, *, session_id, question_id, answer, sequence_no):
        if answer == "poison":
            raise RuntimeError("forced projection failure")
        return real(
            db,
            session_id=session_id,
            question_id=question_id,
            answer=answer,
            sequence_no=sequence_no,
        )

    monkeypatch.setattr(recovery_service, "apply_answer_projection", flaky)
    report = reconcile(
        test_client, s["session_id"], [buf("good-1", q0, "ok"), buf("bad-1", q1, "poison")]
    )
    assert report["newly_reconciled_client_event_ids"] == ["good-1"]
    assert report["missing_client_event_ids"] == ["bad-1"]
    assert report["rejected_reasons"]["bad-1"] == "projection_failed"
    assert report["reconciliation_complete"] is False
    assert report["status"] == "RECOVERING"
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0
    # Both events are durable; only the projection of the poisoned item failed.
    assert count_events(factory, event_type="ANSWER_SAVED") == 2


def test_f1_race_lost_identical_is_already_acknowledged(client, monkeypatch):
    """F1: append_event() returns appended=False for an initially-absent key."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    real_append = recovery_service.event_ledger.append_event
    state = {"raced": False}

    def racy(db, **kwargs):
        if kwargs.get("client_event_id") == "k-race" and not state["raced"]:
            state["raced"] = True
            # Competing request wins the key with an IDENTICAL payload.
            res = real_append(db, **kwargs)
            return AppendResult(event=res.event, appended=False)
        return real_append(db, **kwargs)

    monkeypatch.setattr(recovery_service.event_ledger, "append_event", racy)
    report = reconcile(test_client, s["session_id"], [buf("k-race", q, "A")])
    assert report["newly_reconciled_client_event_ids"] == []
    assert report["already_acknowledged_client_event_ids"] == ["k-race"]
    assert report["mismatched_client_event_ids"] == []
    assert count_events(factory, session_id=s["session_id"], client_event_id="k-race") == 1
    row = response_row(factory, s["session_id"], q)
    assert row is not None and row.current_answer == "A"


def test_f1_race_lost_conflicting_is_mismatch(client, monkeypatch):
    """F1: raced key with a DIFFERENT authoritative payload -> mismatch."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    real_append = recovery_service.event_ledger.append_event
    state = {"raced": False}

    def racy(db, **kwargs):
        if kwargs.get("client_event_id") == "k-race" and not state["raced"]:
            state["raced"] = True
            winner_kwargs = dict(kwargs)
            winner_kwargs["payload"] = {"question_id": q, "answer": "WINNER"}
            res = real_append(db, **winner_kwargs)
            return AppendResult(event=res.event, appended=False)
        return real_append(db, **kwargs)

    monkeypatch.setattr(recovery_service.event_ledger, "append_event", racy)
    report = reconcile(test_client, s["session_id"], [buf("k-race", q, "OTHER")])
    assert report["mismatched_client_event_ids"] == ["k-race"]
    assert report["newly_reconciled_client_event_ids"] == []
    assert report["reconciliation_complete"] is False
    assert count_events(factory, session_id=s["session_id"], client_event_id="k-race") == 1
    assert response_row(factory, s["session_id"], q) is None

def test_f2_ledger_append_failure_is_contained(client, monkeypatch):
    """F2: one item's ledger failure must not poison the rest of the batch."""
    test_client, factory = client
    ids = seed(factory)
    q0, q1 = ids["question_ids"]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    real_append = recovery_service.event_ledger.append_event

    def poisoned(db, **kwargs):
        if kwargs.get("client_event_id") == "k-poison":
            raise RuntimeError("forced ledger failure")
        return real_append(db, **kwargs)

    monkeypatch.setattr(recovery_service.event_ledger, "append_event", poisoned)
    report = reconcile(
        test_client, s["session_id"], [buf("k-good", q0, "A"), buf("k-poison", q1, "B")]
    )
    assert report["newly_reconciled_client_event_ids"] == ["k-good"]
    assert report["missing_client_event_ids"] == ["k-poison"]
    assert report["rejected_reasons"]["k-poison"] == "ledger_append_failed"
    assert report["reconciliation_complete"] is False
    assert report["status"] == "RECOVERING"
    assert count_events(factory, session_id=s["session_id"], client_event_id="k-poison") == 0
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0

    monkeypatch.undo()
    second = reconcile(test_client, s["session_id"], [buf("k-poison", q1, "B")])
    assert second["newly_reconciled_client_event_ids"] == ["k-poison"]
    assert second["reconciliation_complete"] is True
    assert second["status"] == "ACTIVE"


def test_t2_post_commit_error_identical_is_already_acknowledged(client, monkeypatch):
    """T2: commit succeeded then raised; re-query resolves to ack + repair."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    real_append = recovery_service.event_ledger.append_event
    state = {"raised": False}

    def commit_then_raise(db, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            real_append(db, **kwargs)  # durable commit
            raise RuntimeError("post-commit failure")
        return real_append(db, **kwargs)

    monkeypatch.setattr(recovery_service.event_ledger, "append_event", commit_then_raise)
    report = reconcile(test_client, s["session_id"], [buf("k-t2", q, "A")])
    assert report["rejected_reasons"].get("k-t2") != "ledger_append_failed"
    assert report["already_acknowledged_client_event_ids"] == ["k-t2"]
    assert report["missing_client_event_ids"] == []
    assert count_events(factory, session_id=s["session_id"], client_event_id="k-t2") == 1
    row = response_row(factory, s["session_id"], q)
    assert row is not None and row.current_answer == "A"
    assert report["reconciliation_complete"] is True
    assert report["status"] == "ACTIVE"


def test_t2_post_commit_error_conflicting_is_mismatch(client, monkeypatch):
    """T2: committed winner + raised error + different payload -> mismatch."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)

    real_append = recovery_service.event_ledger.append_event
    state = {"raised": False}

    def commit_then_raise(db, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            winner_kwargs = dict(kwargs)
            winner_kwargs["payload"] = {"question_id": q, "answer": "WINNER"}
            real_append(db, **winner_kwargs)
            raise RuntimeError("post-commit failure")
        return real_append(db, **kwargs)

    monkeypatch.setattr(recovery_service.event_ledger, "append_event", commit_then_raise)
    report = reconcile(test_client, s["session_id"], [buf("k-t2", q, "OTHER")])
    assert report["mismatched_client_event_ids"] == ["k-t2"]
    assert "k-t2" not in report["rejected_reasons"]
    assert count_events(factory, session_id=s["session_id"], client_event_id="k-t2") == 1
    assert response_row(factory, s["session_id"], q) is None


def test_t3_intra_request_conflict_is_mismatch(client):
    """T3: same client_event_id twice with different payload -> mismatch."""
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(
        test_client, s["session_id"], [buf("dup-9", q, "A"), buf("dup-9", q, "B")]
    )
    assert report["newly_reconciled_client_event_ids"] == ["dup-9"]
    assert report["mismatched_client_event_ids"] == ["dup-9"]
    assert report["reconciliation_complete"] is False
    assert count_events(factory, session_id=s["session_id"], client_event_id="dup-9") == 1
    row = response_row(factory, s["session_id"], q)
    assert row is not None and row.current_answer == "A"


def test_r25_heartbeat_allowed_but_does_not_advance_activity(client):
    """RECOVERING permits heartbeat as a liveness signal only."""
    test_client, factory = client
    ids = seed(factory)
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    before = test_client.get(f"/session/{s['session_id']}/state").json()["last_activity"]
    r = test_client.post(
        f"/session/{s['session_id']}/heartbeat", json={"client_event_id": "hb-r1"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["appended"] is True
    assert count_events(factory, event_type="HEARTBEAT") == 1
    after = test_client.get(f"/session/{s['session_id']}/state").json()["last_activity"]
    assert after == before  # write-readiness is NOT implied during recovery
    db = factory()
    try:
        assert db.get(Session, s["session_id"]).status == "RECOVERING"
    finally:
        db.close()


def test_r26_recovery_uses_same_shared_helper_object(client):
    """Single source of truth: both services bind the SAME helper function."""
    from app.services import recovery_service, response_projection, session_service

    assert (
        recovery_service.apply_answer_projection
        is response_projection.apply_answer_projection
    )
    assert (
        session_service.response_projection.apply_answer_projection
        is response_projection.apply_answer_projection
    )
    # And Task 5 still projects through it end-to-end.
    test_client, factory = client
    ids = seed(factory)
    q = ids["question_ids"][0]
    s = _to_recovering(test_client, factory, ids, with_answer=False)
    report = reconcile(test_client, s["session_id"], [buf("b-1", q, "A")])
    assert report["reconciliation_complete"] is True
    assert report["status"] == "ACTIVE"
    row = response_row(factory, s["session_id"], q)
    assert row.current_answer == "A" and row.last_event_id is not None