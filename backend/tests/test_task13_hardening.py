"""Task 13 regression tests: state, incident, session and API hardening.

Every behaviour changed in Task 13 has a regression test here:

1. RECOVERING -> ACTIVE with an EMPTY reconciliation
2. NODE incident detection for an ongoing FAILED node (episode-scoped)
3. node flapping while sessions are RECOVERING
4. duplicate ACTIVE session prevention
5. automatic incident evaluation (no manual POST needed)
6. normalised domain-error -> HTTP mapping on the demo routes
7. AI grounding stays fail-closed (no weakening)
"""

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api.http_errors import raise_mapped
from app.core.database import get_db
from app.db import (
    Base,
    Candidate,
    Event,
    Exam,
    Incident,
    Node,
    Question,
    Session,
)
from app.main import app
from app.services import ai_incident_service, incident_service


@pytest.fixture()
def client(tmp_path):
    """TestClient on an isolated DB."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_task13.db",
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


def seed(
    factory,
    node_count=1,
    sessions_per_node=0,
    candidates=3,
    questions=1,
    title="T13",
):
    """Exam + nodes + candidates + questions (+ optional sessions).

    Sessions default to 0: tests create them through POST /session/start so the
    ledger-backed SESSION_STARTED invariants always hold.
    """
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title=title, status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        nodes = [Node(exam_id=exam.id, status="HEALTHY") for _ in range(node_count)]
        qs = [Question(exam_id=exam.id, text=f"Q{i}?", marks=1) for i in range(questions)]
        db.add_all(nodes + qs)
        db.flush()
        cands = []
        for i in range(candidates):
            c = Candidate(name=f"C{i}", roll_no=f"{title}-{exam.id}-{i}")
            db.add(c)
            db.flush()
            cands.append(c.id)
        session_ids = []
        idx = 0
        for node in nodes:
            for _ in range(sessions_per_node):
                sess = Session(
                    exam_id=exam.id,
                    candidate_id=cands[idx % len(cands)],
                    node_id=node.id,
                    status="ACTIVE",
                    started_at=now,
                )
                db.add(sess)
                db.flush()
                session_ids.append(sess.id)
                idx += 1
        db.commit()
        return {
            "exam_id": exam.id,
            "node_ids": [n.id for n in nodes],
            "question_ids": [q.id for q in qs],
            "candidate_ids": cands,
            "session_ids": session_ids,
        }
    finally:
        db.close()


def start(tc, exam_id, candidate_id, node_id):
    r = tc.post(
        "/session/start",
        json={"exam_id": exam_id, "candidate_id": candidate_id, "node_id": node_id},
    )
    assert r.status_code == 200, r.text
    return r.json()


def fail(tc, node_id):
    r = tc.post(f"/demo/nodes/{node_id}/fail", json={"reason": "t13"})
    assert r.status_code == 200, r.text
    return r.json()


def recover(tc, node_id):
    r = tc.post(f"/demo/nodes/{node_id}/recover", json={"reason": "t13"})
    assert r.status_code == 200, r.text
    return r.json()


def reconcile(tc, session_id, events):
    r = tc.post(f"/session/{session_id}/reconcile", json={"events": events})
    assert r.status_code == 200, r.text
    return r.json()


def status_of(factory, session_id):
    db = factory()
    try:
        return db.get(Session, session_id).status
    finally:
        db.close()


def count_events(factory, **filters):
    db = factory()
    try:
        stmt = select(func.count()).select_from(Event)
        for k, v in filters.items():
            stmt = stmt.where(getattr(Event, k) == v)
        return db.scalar(stmt)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. RECOVERING -> ACTIVE with an empty reconciliation
# --------------------------------------------------------------------------- #


def _to_recovering(tc, factory, ids, node_id, session_id):
    """Drive one session ACTIVE -> DISCONNECTED -> RECOVERING."""
    fail(tc, node_id)
    assert status_of(factory, session_id) == "DISCONNECTED"
    recover(tc, node_id)
    assert status_of(factory, session_id) == "RECOVERING"
    return session_id


def test_empty_reconciliation_activates_session(client):
    """A clean empty buffer is a SUCCESSFUL recovery, not a stuck session."""
    tc, factory = client
    ids = seed(factory, questions=1)
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], ids["node_ids"][0])
    _to_recovering(tc, factory, ids, ids["node_ids"][0], s["session_id"])

    report = reconcile(tc, s["session_id"], [])
    assert report["reconciliation_complete"] is True
    assert report["status"] == "ACTIVE"
    assert report["recovered_event_sequence_no"] is not None
    assert status_of(factory, s["session_id"]) == "ACTIVE"


def test_empty_reconciliation_creates_one_session_recovered_event(client):
    """Exactly one SESSION_RECOVERED event is appended for the clean recovery."""
    tc, factory = client
    ids = seed(factory, questions=1)
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], ids["node_ids"][0])
    _to_recovering(tc, factory, ids, ids["node_ids"][0], s["session_id"])
    before = count_events(factory, event_type="SESSION_RECOVERED")

    reconcile(tc, s["session_id"], [])

    assert count_events(factory, event_type="SESSION_RECOVERED") == before + 1
    event = db_event(factory, s["session_id"], "SESSION_RECOVERED")
    assert event is not None
    assert event.payload["reconciled_event_ids"] == []
    assert event.payload["resulting_status"] == "ACTIVE"


def db_event(factory, session_id, event_type):
    db = factory()
    try:
        return db.scalars(
            select(Event).where(
                Event.session_id == session_id, Event.event_type == event_type
            )
        ).first()
    finally:
        db.close()


def test_answer_is_accepted_after_empty_reconciliation(client):
    """The session is genuinely usable again, not merely relabelled."""
    tc, factory = client
    ids = seed(factory, questions=1)
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], ids["node_ids"][0])
    _to_recovering(tc, factory, ids, ids["node_ids"][0], s["session_id"])
    reconcile(tc, s["session_id"], [])

    r = tc.post(
        f"/session/{s['session_id']}/answer",
        json={
            "question_id": ids["question_ids"][0],
            "answer": "A",
            "client_event_id": "after-empty",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_answer"] == "A"


def test_repeated_empty_reconciliation_does_not_duplicate_recovery(client):
    """Re-reconciling never appends a second SESSION_RECOVERED.

    A second call on an ACTIVE session is a state conflict (409) under the
    preserved state rules; the guarantee is that no duplicate recovery event is
    ever written.
    """
    tc, factory = client
    ids = seed(factory, questions=1)
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], ids["node_ids"][0])
    _to_recovering(tc, factory, ids, ids["node_ids"][0], s["session_id"])
    reconcile(tc, s["session_id"], [])
    assert count_events(factory, event_type="SESSION_RECOVERED") == 1

    again = tc.post(f"/session/{s['session_id']}/reconcile", json={"events": []})
    assert again.status_code == 409
    assert count_events(factory, event_type="SESSION_RECOVERED") == 1
    assert status_of(factory, s["session_id"]) == "ACTIVE"


def test_failed_reconciliation_still_does_not_activate(client):
    """Strict failure behaviour is preserved: a mismatch keeps RECOVERING."""
    tc, factory = client
    ids = seed(factory, questions=1)
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], ids["node_ids"][0])
    qid = ids["question_ids"][0]
    _to_recovering(tc, factory, ids, ids["node_ids"][0], s["session_id"])

    # A duplicate id with conflicting payloads is a mismatch.
    report = reconcile(
        tc,
        s["session_id"],
        [
            {"client_event_id": "dup", "question_id": qid, "answer": "A"},
            {"client_event_id": "dup", "question_id": qid, "answer": "B"},
        ],
    )
    assert report["reconciliation_complete"] is False
    assert report["mismatched_client_event_ids"] == ["dup"]
    assert report["status"] == "RECOVERING"
    assert status_of(factory, s["session_id"]) == "RECOVERING"
    assert count_events(factory, event_type="SESSION_RECOVERED") == 0


# --------------------------------------------------------------------------- #
# 3. Node flapping while sessions are RECOVERING
# --------------------------------------------------------------------------- #


def test_second_failure_disconnects_recovering_sessions(client):
    """ACTIVE -> DISCONNECTED -> RECOVERING -> DISCONNECTED on a re-failure."""
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    s = start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)
    session_id = s["session_id"]

    fail(tc, node_id)
    assert status_of(factory, session_id) == "DISCONNECTED"
    recover(tc, node_id)
    assert status_of(factory, session_id) == "RECOVERING"

    # Node flaps and fails again BEFORE reconciliation completed.
    second = fail(tc, node_id)
    assert second["new_status"] == "FAILED"
    assert session_id in second["affected_session_ids"], (
        "a RECOVERING session must be affected by a second node failure"
    )
    assert status_of(factory, session_id) == "DISCONNECTED"


def test_failure_payload_lists_every_affected_session(client):
    """The NODE_FAILURE_INJECTED payload names ACTIVE and RECOVERING victims."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=2)
    node_id = ids["node_ids"][0]
    a = start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)["session_id"]
    b = start(tc, ids["exam_id"], ids["candidate_ids"][1], node_id)["session_id"]

    fail(tc, node_id)  # both ACTIVE -> DISCONNECTED
    recover(tc, node_id)  # both -> RECOVERING
    second = fail(tc, node_id)  # both RECOVERING -> DISCONNECTED

    assert sorted(second["affected_session_ids"]) == sorted([a, b])
    assert second["affected_count"] == 2

    db = factory()
    try:
        events = db.scalars(
            select(Event)
            .where(Event.event_type == "NODE_FAILURE_INJECTED")
            .order_by(Event.sequence_no.asc())
        ).all()
        assert len(events) == 2, "flapping failure appends a second real event"
        # The second payload reports both RECOVERING victims.
        assert sorted(events[1].payload["affected_session_ids"]) == sorted([a, b])
        assert events[1].payload["affected_session_count"] == 2
    finally:
        db.close()


def test_recovery_after_second_failure_returns_sessions_to_recovering(client):
    """A later recovery moves the re-disconnected sessions back to RECOVERING."""
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    session_id = start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)[
        "session_id"
    ]

    fail(tc, node_id)
    recover(tc, node_id)
    assert status_of(factory, session_id) == "RECOVERING"
    fail(tc, node_id)
    assert status_of(factory, session_id) == "DISCONNECTED"

    recover(tc, node_id)
    assert status_of(factory, session_id) == "RECOVERING"

    # No session is stranded in RECOVERING: it can finish its recovery.
    report = reconcile(tc, session_id, [])
    assert report["reconciliation_complete"] is True
    assert status_of(factory, session_id) == "ACTIVE"


def test_repeated_failure_stays_idempotent(client):
    """Failing an already-FAILED node changes nothing and adds no event."""
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    session_id = start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)[
        "session_id"
    ]
    first = fail(tc, node_id)
    before = count_events(factory, event_type="NODE_FAILURE_INJECTED")

    second = fail(tc, node_id)
    assert second["newly_failed"] is False
    assert second["affected_session_ids"] == first["affected_session_ids"]
    assert count_events(factory, event_type="NODE_FAILURE_INJECTED") == before
    assert status_of(factory, session_id) == "DISCONNECTED"



# --------------------------------------------------------------------------- #
# 4. Duplicate ACTIVE session prevention
# --------------------------------------------------------------------------- #


def test_repeated_start_same_candidate_same_exam_is_idempotent(client):
    """A duplicate start returns the SAME session and adds no second event."""
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    cid = ids["candidate_ids"][0]

    first = start(tc, ids["exam_id"], cid, node_id)
    before = count_events(factory, event_type="SESSION_STARTED")

    second = start(tc, ids["exam_id"], cid, node_id)

    assert second["session_id"] == first["session_id"]
    assert second["status"] == "ACTIVE"
    assert second["event_sequence_no"] == first["event_sequence_no"]
    assert count_events(factory, event_type="SESSION_STARTED") == before

    db = factory()
    try:
        live = db.scalars(
            select(Session).where(
                Session.exam_id == ids["exam_id"],
                Session.candidate_id == cid,
                Session.status == "ACTIVE",
            )
        ).all()
        assert len(live) == 1, "no second ACTIVE session for the same candidate"
    finally:
        db.close()


def test_different_candidates_get_different_sessions(client):
    """Different candidates in the same exam are unaffected."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=2)
    node_id = ids["node_ids"][0]

    a = start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)
    b = start(tc, ids["exam_id"], ids["candidate_ids"][1], node_id)

    assert a["session_id"] != b["session_id"]
    assert a["candidate_id"] != b["candidate_id"]
    assert count_events(factory, event_type="SESSION_STARTED") == 2


def test_same_candidate_different_exam_gets_new_session(client):
    """The same candidate may legitimately sit a different exam."""
    tc, factory = client
    a = seed(factory, questions=1, title="A")
    b = seed(factory, questions=1, title="B")

    first = start(tc, a["exam_id"], a["candidate_ids"][0], a["node_ids"][0])
    db = factory()
    try:
        # Attach the very same candidate row to exam B.
        cand = db.get(Candidate, a["candidate_ids"][0])
        assert cand.id == a["candidate_ids"][0]
    finally:
        db.close()
    second = start(tc, b["exam_id"], a["candidate_ids"][0], b["node_ids"][0])

    assert second["session_id"] != first["session_id"]
    assert second["exam_id"] == b["exam_id"]


def test_terminal_session_allows_a_new_sitting(client):
    """A SUBMITTED session is terminal, so a fresh start is legitimate."""
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    cid = ids["candidate_ids"][0]
    first = start(tc, ids["exam_id"], cid, node_id)

    db = factory()
    try:
        sess = db.get(Session, first["session_id"])
        sess.status = "SUBMITTED"
        db.commit()
    finally:
        db.close()

    second = start(tc, ids["exam_id"], cid, node_id)
    assert second["session_id"] != first["session_id"]
    assert second["status"] == "ACTIVE"
    # The terminal session is untouched.
    assert status_of(factory, first["session_id"]) == "SUBMITTED"



# --------------------------------------------------------------------------- #
# 5. Automatic incident evaluation
# --------------------------------------------------------------------------- #


def _incidents(tc, exam_id):
    r = tc.get("/incidents", params={"exam_id": exam_id})
    assert r.status_code == 200, r.text
    return r.json()["incidents"]


def test_fail_node_creates_incidents_without_manual_evaluation(client):
    """fail_node alone makes incidents available; no manual POST is needed."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=3)
    node_id = ids["node_ids"][0]
    for cid in ids["candidate_ids"]:
        start(tc, ids["exam_id"], cid, node_id)

    assert _incidents(tc, ids["exam_id"]) == []

    fail(tc, node_id)

    incidents = _incidents(tc, ids["exam_id"])
    assert incidents, "incidents must appear without an explicit evaluate call"
    assert {i["severity"] for i in incidents} == {"CANDIDATE", "NODE", "SYSTEM"}
    assert {i["status"] for i in incidents} == {"ACTIVE"}
    for incident in incidents:
        assert incident["evidence_event_sequence_nos"]


def test_recovery_and_reconcile_resolve_without_manual_evaluation(client):
    """Recovery + reconciliation resolve incidents on their own."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=3)
    node_id = ids["node_ids"][0]
    sessions = [
        start(tc, ids["exam_id"], cid, node_id)["session_id"]
        for cid in ids["candidate_ids"]
    ]
    fail(tc, node_id)
    recover(tc, node_id)
    for session_id in sessions:
        reconcile(tc, session_id, [])

    incidents = _incidents(tc, ids["exam_id"])
    assert {i["status"] for i in incidents} == {"RESOLVED"}
    assert {status_of(factory, s) for s in sessions} == {"ACTIVE"}


def test_read_endpoints_do_not_mutate_incidents(client):
    """GET /incidents and the overview never perform hidden writes."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=3)
    node_id = ids["node_ids"][0]
    for cid in ids["candidate_ids"]:
        start(tc, ids["exam_id"], cid, node_id)
    fail(tc, node_id)

    def snapshot():
        db = factory()
        try:
            return set(
                db.scalars(
                    select(Incident.id).where(Incident.exam_id == ids["exam_id"])
                ).all()
            )
        finally:
            db.close()

    before = snapshot()
    for _ in range(3):
        assert tc.get("/incidents", params={"exam_id": ids["exam_id"]}).status_code == 200
        assert tc.get(f"/demo/overview/{ids['exam_id']}").status_code == 200
    assert snapshot() == before


def test_repeated_triggered_evaluation_is_idempotent(client):
    """Failure/recovery triggers never duplicate an incident."""
    tc, factory = client
    ids = seed(factory, questions=1, candidates=3)
    node_id = ids["node_ids"][0]
    for cid in ids["candidate_ids"]:
        start(tc, ids["exam_id"], cid, node_id)

    fail(tc, node_id)
    first = {(i["severity"], i["id"]) for i in _incidents(tc, ids["exam_id"])}
    assert len(first) == 5  # 3 CANDIDATE + 1 NODE + 1 SYSTEM

    # Repeated operations that re-trigger evaluation must not add rows.
    fail(tc, node_id)
    recover(tc, node_id)
    for _ in range(2):
        evaluate = tc.post(
            "/demo/incidents/evaluate", json={"exam_id": ids["exam_id"]}
        )
        assert evaluate.status_code == 200

    ids_seen = {i["id"] for i in _incidents(tc, ids["exam_id"])}
    assert ids_seen == {i_id for _, i_id in first}, "no duplicate incident was created"



# --------------------------------------------------------------------------- #
# 6. Normalised domain-error -> HTTP mapping on the demo routes
# --------------------------------------------------------------------------- #


def test_raise_mapped_status_table():
    """The shared mapper produces the documented status for each domain error."""
    from fastapi import HTTPException

    from app.api.errors import (
        ForeignExamQuestion,
        InvalidNodeState,
        InvalidState,
        NodeUnavailable,
        NotFound,
        SessionApiError,
    )

    cases = [
        (NotFound("node"), 404),
        (NodeUnavailable(1, 2, "answer"), 503),
        (InvalidState("RECOVERING"), 409),
        (ForeignExamQuestion(), 422),
        (SessionApiError("boom"), 500),
        (InvalidNodeState(1, "MAINTENANCE"), 500),
    ]
    for exc, expected in cases:
        with pytest.raises(HTTPException) as excinfo:
            raise_mapped(exc)
        assert excinfo.value.status_code == expected, exc


def test_demo_routes_not_found_404(client):
    tc, _ = client
    assert tc.post("/demo/nodes/99999/fail", json={}).status_code == 404
    assert tc.post("/demo/nodes/99999/recover", json={}).status_code == 404
    assert tc.get("/demo/overview/99999").status_code == 404
    assert tc.post("/demo/tamper", json={"target": "latest", "field": "payload"}).status_code == 404


def test_demo_routes_state_conflict_409(client):
    tc, factory = client
    ids = seed(factory, questions=1)
    node_id = ids["node_ids"][0]
    start(tc, ids["exam_id"], ids["candidate_ids"][0], node_id)

    # A node outside the failable/recoverable sets is a state conflict.
    db = factory()
    try:
        db.get(Node, node_id).status = "MAINTENANCE"
        db.commit()
    finally:
        db.close()
    assert tc.post(f"/demo/nodes/{node_id}/recover", json={}).status_code == 409
    assert tc.post(f"/demo/nodes/{node_id}/fail", json={}).status_code == 409

    # A FAILED node recovers, and repeating that transition is idempotent.
    db = factory()
    try:
        db.get(Node, node_id).status = "FAILED"
        db.commit()
    finally:
        db.close()
    first = tc.post(f"/demo/nodes/{node_id}/recover", json={})
    assert first.status_code == 200
    again = tc.post(f"/demo/nodes/{node_id}/recover", json={})
    assert again.status_code == 200
    assert again.json()["newly_recovered"] is False


def test_demo_route_unexpected_domain_error_is_generic_500(client, monkeypatch):
    """An unexpected domain error yields a generic 500 with no internal detail."""
    from app.api import demo as demo_api
    from app.api.errors import SessionApiError

    tc, factory = client
    ids = seed(factory, questions=1)

    def boom(*args, **kwargs):
        raise SessionApiError("secret internal table name leaked")

    monkeypatch.setattr(demo_api.failure_service, "fail_node", boom)
    r = tc.post(f"/demo/nodes/{ids['node_ids'][0]}/fail", json={})
    assert r.status_code == 500
    assert "secret internal table name" not in r.text
    assert "Traceback" not in r.text
    assert r.json()["detail"] == "internal error"


def test_demo_tamper_validation_errors_are_400(client):
    tc, factory = client
    seed(factory, questions=1)
    assert tc.post("/demo/tamper", json={"target": "7", "field": "payload"}).status_code == 400
    assert tc.post("/demo/tamper", json={"target": "latest", "field": "nope"}).status_code == 400


# --------------------------------------------------------------------------- #
# 7. AI grounding stays fail-closed (must not be weakened)
# --------------------------------------------------------------------------- #


def _analysis(refs):
    return ai_incident_service.IncidentAnalysis(
        likely_cause="c",
        impact_summary="i",
        recommended_response="r",
        evidence_refs=list(refs),
    )


def test_empty_evidence_refs_is_rejected():
    """No fallback citation is invented: empty refs remain a controlled error."""
    with pytest.raises(ai_incident_service.AIResponseInvalidError):
        ai_incident_service._ground_evidence_refs(_analysis([]), [1, 2, 3])


def test_ungrounded_evidence_refs_rejected_not_filtered():
    """Invalid refs reject the response; they are never silently filtered out."""
    with pytest.raises(ai_incident_service.AIResponseInvalidError) as excinfo:
        ai_incident_service._ground_evidence_refs(_analysis([1, 777]), [1, 2, 3])
    assert "777" in str(excinfo.value)


def test_grounded_evidence_refs_still_accepted():
    assert ai_incident_service._ground_evidence_refs(_analysis([3, 1]), [1, 2, 3]) == [1, 3]
