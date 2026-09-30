"""End-to-end resilience demo tests (Task 9).

Drives the whole walkthrough over real HTTP against an isolated SQLite database:

    create scenario -> start 3 sessions -> save answers -> fail node
    -> evaluate incidents -> recover node -> reconcile buffered answers
    -> evaluate again -> sessions ACTIVE + incidents resolved
    -> verify audit -> fetch evidence -> mocked Gemini analyze
    -> overview reflects every transition

The suite also proves that scenario creation never bypasses the normal
session-start / event-ledger path.
"""

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.db import (
    Base,
    Candidate,
    Event,
    Exam,
    Incident,
    Node,
    Question,
    Response,
    Session,
)
from app.main import app
from app.services import ai_incident_service

BUFFERS_PER_SESSION = 2


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on an isolated DB, with a dummy Gemini key configured."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_demo_scenario.db",
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
    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "test-key")
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def create_scenario(tc):
    r = tc.post("/demo/scenario/create")
    assert r.status_code == 200, r.text
    return r.json()


def overview(tc, exam_id):
    r = tc.get(f"/demo/overview/{exam_id}")
    assert r.status_code == 200, r.text
    return r.json()


def start_session(tc, exam_id, candidate_id, node_id):
    r = tc.post(
        "/session/start",
        json={
            "exam_id": exam_id,
            "candidate_id": candidate_id,
            "node_id": node_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def save_answer(tc, session_id, question_id, answer, client_event_id):
    r = tc.post(
        f"/session/{session_id}/answer",
        json={
            "question_id": question_id,
            "answer": answer,
            "client_event_id": client_event_id,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def fail_node(tc, node_id):
    r = tc.post(f"/demo/nodes/{node_id}/fail", json={"reason": "demo failure"})
    assert r.status_code == 200, r.text
    return r.json()


def recover_node(tc, node_id):
    r = tc.post(f"/demo/nodes/{node_id}/recover", json={"reason": "demo recovery"})
    assert r.status_code == 200, r.text
    return r.json()


def evaluate(tc, exam_id):
    r = tc.post("/demo/incidents/evaluate", json={"exam_id": exam_id})
    assert r.status_code == 200, r.text
    return r.json()["incidents"]


def reconcile(tc, session_id, items):
    r = tc.post(f"/session/{session_id}/reconcile", json={"events": items})
    assert r.status_code == 200, r.text
    return r.json()


def count(factory, model):
    db = factory()
    try:
        return db.scalar(select(func.count()).select_from(model))
    finally:
        db.close()


def event_types(factory):
    db = factory()
    try:
        return [
            ev.event_type
            for ev in db.scalars(
                select(Event).order_by(Event.sequence_no.asc())
            ).all()
        ]
    finally:
        db.close()


def mock_gemini(monkeypatch, evidence_refs):
    payload = json.dumps(
        {
            "likely_cause": "The exam node crashed mid-exam.",
            "impact_summary": "Three candidates lost connectivity.",
            "recommended_response": "Recover the node and reconcile buffered answers.",
            "evidence_refs": list(evidence_refs),
        }
    )

    class _Part:
        text = payload

    class _Content:
        parts = [_Part()]

    class _Candidate:
        content = _Content()

    class _Response:
        parsed = None
        candidates = [_Candidate()]

    monkeypatch.setattr(
        ai_incident_service, "_call_gemini", lambda _prompt: _Response()
    )



# --------------------------------------------------------------------------- #
# Scenario creation
# --------------------------------------------------------------------------- #


def test_create_scenario_returns_expected_shape(client):
    """Exactly one exam, one node, three candidates and 15 questions."""
    tc, factory = client
    s = create_scenario(tc)

    assert set(s) == {"exam_id", "node_id", "candidate_ids", "question_ids"}
    assert isinstance(s["exam_id"], int)
    assert isinstance(s["node_id"], int)
    assert len(s["candidate_ids"]) == 3
    assert len(s["question_ids"]) == 15
    assert len(set(s["candidate_ids"])) == 3
    assert len(set(s["question_ids"])) == 15

    assert count(factory, Exam) == 1
    assert count(factory, Node) == 1
    assert count(factory, Candidate) == 3
    assert count(factory, Question) == 15


def test_create_scenario_node_is_healthy(client):
    """The provisioned node starts HEALTHY so sessions can be started."""
    tc, _ = client
    s = create_scenario(tc)
    snap = overview(tc, s["exam_id"])
    assert [n["id"] for n in snap["nodes"]] == [s["node_id"]]
    assert snap["nodes"][0]["status"] == "HEALTHY"


def test_create_scenario_creates_no_sessions_and_no_events(client):
    """Scenario creation must NOT create sessions or touch the ledger."""
    tc, factory = client
    create_scenario(tc)

    assert count(factory, Session) == 0
    assert count(factory, Response) == 0
    assert count(factory, Event) == 0
    assert count(factory, Incident) == 0
    assert event_types(factory) == []


def test_create_scenario_does_not_bypass_session_start_path(client):
    """Sessions (and SESSION_STARTED) only appear via POST /session/start."""
    tc, factory = client
    s = create_scenario(tc)
    assert count(factory, Session) == 0
    assert count(factory, Event) == 0

    session = start_session(tc, s["exam_id"], s["candidate_ids"][0], s["node_id"])

    assert count(factory, Session) == 1
    events = event_types(factory)
    assert events == ["SESSION_STARTED"], (
        "the only event must be the real SESSION_STARTED from the normal path"
    )
    # The scenario endpoint itself returned no session id to bypass with.
    assert "session_ids" not in s
    assert "session_id" not in s
    assert session["status"] == "ACTIVE"
    assert session["event_sequence_no"] == 1


def test_create_scenario_is_additive_and_does_not_delete(client):
    """A second scenario neither deletes nor alters the first one."""
    tc, factory = client
    first = create_scenario(tc)
    start_session(tc, first["exam_id"], first["candidate_ids"][0], first["node_id"])

    second = create_scenario(tc)

    assert second["exam_id"] != first["exam_id"]
    assert second["node_id"] != first["node_id"]
    assert count(factory, Exam) == 2
    assert count(factory, Node) == 2
    assert count(factory, Candidate) == 6
    assert count(factory, Question) == 30
    # The first scenario's session and its event survive untouched.
    assert count(factory, Session) == 1
    assert event_types(factory) == ["SESSION_STARTED"]
    first_snap = overview(tc, first["exam_id"])
    assert len(first_snap["sessions"]) == 1



def test_create_scenario_roll_numbers_are_unique(client):
    """Two scenarios never collide on the unique roll_no constraint."""
    tc, factory = client
    create_scenario(tc)
    r = tc.post("/demo/scenario/create")
    assert r.status_code == 200, r.text
    db = factory()
    try:
        roll_nos = [c.roll_no for c in db.scalars(select(Candidate)).all()]
    finally:
        db.close()
    assert len(roll_nos) == len(set(roll_nos)) == 6


# --------------------------------------------------------------------------- #
# Operational overview
# --------------------------------------------------------------------------- #


def test_overview_unknown_exam_404(client):
    """An unknown exam has no snapshot."""
    tc, _ = client
    r = tc.get("/demo/overview/99999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_overview_fresh_scenario_shape(client):
    """A fresh scenario reports no sessions, no incidents and a null sequence."""
    tc, _ = client
    s = create_scenario(tc)
    snap = overview(tc, s["exam_id"])

    assert set(snap) == {
        "exam",
        "nodes",
        "sessions",
        "incidents",
        "audit_status",
        "latest_event_sequence_no",
    }
    assert snap["exam"]["id"] == s["exam_id"]
    assert snap["sessions"] == []
    assert snap["incidents"] == []
    assert snap["latest_event_sequence_no"] is None
    # Empty ledger is still a valid chain.
    assert snap["audit_status"]["valid"] is True
    assert snap["audit_status"]["events_checked"] == 0


def test_overview_tracks_sessions_and_latest_sequence(client):
    """Sessions and the latest ledger sequence are reflected live."""
    tc, _ = client
    s = create_scenario(tc)
    session = start_session(
        tc, s["exam_id"], s["candidate_ids"][0], s["node_id"]
    )
    save_answer(
        tc, session["session_id"], s["question_ids"][0], "A", "ans-1"
    )

    snap = overview(tc, s["exam_id"])
    assert len(snap["sessions"]) == 1
    row = snap["sessions"][0]
    assert row["id"] == session["session_id"]
    assert row["candidate_id"] == s["candidate_ids"][0]
    assert row["node_id"] == s["node_id"]
    assert row["status"] == "ACTIVE"
    assert row["last_activity"] is not None
    assert snap["latest_event_sequence_no"] == 2  # SESSION_STARTED + ANSWER_SAVED


def test_overview_is_scoped_to_its_own_exam(client):
    """Sessions of another exam never leak into this snapshot."""
    tc, _ = client
    a = create_scenario(tc)
    b = create_scenario(tc)
    start_session(tc, a["exam_id"], a["candidate_ids"][0], a["node_id"])

    snap = overview(tc, b["exam_id"])
    assert snap["sessions"] == []
    assert [n["id"] for n in snap["nodes"]] == [b["node_id"]]
    # The ledger sequence number is global, not exam-scoped.
    assert snap["latest_event_sequence_no"] == 1


def test_overview_never_calls_gemini(client, monkeypatch):
    """The overview endpoint is read-only and must not reach Gemini."""
    tc, _ = client
    s = create_scenario(tc)

    def explode(_prompt):
        raise AssertionError("the overview endpoint must never call Gemini")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", explode)
    r = tc.get(f"/demo/overview/{s['exam_id']}")
    assert r.status_code == 200, r.text


def test_overview_is_read_only(client):
    """Fetching the overview writes nothing."""
    tc, factory = client
    s = create_scenario(tc)
    start_session(tc, s["exam_id"], s["candidate_ids"][0], s["node_id"])

    before = (
        count(factory, Exam),
        count(factory, Node),
        count(factory, Candidate),
        count(factory, Question),
        count(factory, Session),
        count(factory, Response),
        count(factory, Event),
        count(factory, Incident),
    )
    overview(tc, s["exam_id"])
    after = (
        count(factory, Exam),
        count(factory, Node),
        count(factory, Candidate),
        count(factory, Question),
        count(factory, Session),
        count(factory, Response),
        count(factory, Event),
        count(factory, Incident),
    )
    assert before == after



# --------------------------------------------------------------------------- #
# Full end-to-end resilience walkthrough (real HTTP only)
# --------------------------------------------------------------------------- #


def start_and_answer(tc, s):
    """Start all three sessions and save one answer on each."""
    sessions = [
        start_session(tc, s["exam_id"], candidate_id, s["node_id"])
        for candidate_id in s["candidate_ids"]
    ]
    for index, session in enumerate(sessions):
        save_answer(
            tc, session["session_id"], s["question_ids"][0], "A", f"live-{index}"
        )
    return sessions


def run_to_failure(tc, s):
    """start + answer -> fail node -> evaluate."""
    sessions = start_and_answer(tc, s)
    failed = fail_node(tc, s["node_id"])
    incidents = evaluate(tc, s["exam_id"])
    return sessions, failed, incidents


def test_end_to_end_resilience_walkthrough(client, monkeypatch):
    """The complete demo flow, driven purely through the public HTTP API."""
    tc, factory = client
    s = create_scenario(tc)

    # --- 1. three sessions on the healthy node, then live answers ---------
    sessions = start_and_answer(tc, s)
    assert len(sessions) == 3
    assert all(sess["status"] == "ACTIVE" for sess in sessions)
    snap = overview(tc, s["exam_id"])
    assert [row["status"] for row in snap["sessions"]] == ["ACTIVE"] * 3
    assert snap["incidents"] == []
    assert snap["audit_status"]["valid"] is True
    assert snap["latest_event_sequence_no"] == 6  # 3 starts + 3 answers

    # --- 2. node failure disconnects every affected session ---------------
    failed = fail_node(tc, s["node_id"])
    incidents = evaluate(tc, s["exam_id"])
    assert failed["new_status"] == "FAILED"
    assert sorted(failed["affected_session_ids"]) == sorted(
        sess["session_id"] for sess in sessions
    )
    snap = overview(tc, s["exam_id"])
    assert snap["nodes"][0]["status"] == "FAILED"
    assert {row["status"] for row in snap["sessions"]} == {"DISCONNECTED"}

    # --- 3. incidents are detected and grounded ---------------------------
    severities = {inc["severity"] for inc in incidents}
    assert "CANDIDATE" in severities
    assert "NODE" in severities
    # All three sessions are DISCONNECTED, so the affected-session ratio also
    # crosses the SYSTEM threshold: a SYSTEM incident must be projected too.
    system_incidents = [inc for inc in incidents if inc["severity"] == "SYSTEM"]
    assert len(system_incidents) == 1, (
        f"expected exactly one ACTIVE SYSTEM incident, got {severities}"
    )
    system = system_incidents[0]
    assert system["status"] == "ACTIVE"
    assert system["created_from_event_id"] is not None
    assert system["root_cause_summary"].startswith("System-wide outage:")
    assert sorted(system["affected_session_ids"]) == sorted(
        sess["session_id"] for sess in sessions
    )
    assert severities == {"CANDIDATE", "NODE", "SYSTEM"}
    for inc in incidents:
        assert inc["status"] == "ACTIVE"
        assert inc["created_from_event_id"] is not None
        assert inc["evidence_event_sequence_nos"]
    # Every incident is anchored to the same real NODE_FAILURE_INJECTED event.
    origins = {inc["created_from_event_id"] for inc in incidents}
    assert origins == {failed["event_sequence_no"]}
    snap = overview(tc, s["exam_id"])
    assert len(snap["incidents"]) == len(incidents)
    assert {row["id"] for row in snap["incidents"]} == {inc["id"] for inc in incidents}
    assert any(
        row["severity"] == "SYSTEM" and row["status"] == "ACTIVE"
        for row in snap["incidents"]
    )

    # --- 4. recovery moves sessions to RECOVERING -------------------------
    recovered = recover_node(tc, s["node_id"])
    assert recovered["new_status"] == "HEALTHY"
    snap = overview(tc, s["exam_id"])
    assert snap["nodes"][0]["status"] == "HEALTHY"
    assert {row["status"] for row in snap["sessions"]} == {"RECOVERING"}

    # --- 5. buffered answers are reconciled back to ACTIVE ----------------
    for index, session in enumerate(sessions):
        report = reconcile(
            tc,
            session["session_id"],
            [
                {
                    "client_event_id": f"buffered-{index}-{k}",
                    "question_id": s["question_ids"][k],
                    "answer": "B",
                }
                for k in range(BUFFERS_PER_SESSION)
            ],
        )
        assert report["reconciliation_complete"] is True, report
        assert report["recovered_event_sequence_no"] is not None
        assert report["status"] == "ACTIVE"

    snap = overview(tc, s["exam_id"])
    assert {row["status"] for row in snap["sessions"]} == {"ACTIVE"}
    assert snap["nodes"][0]["status"] == "HEALTHY"


    # --- 6. a second evaluation resolves every incident -------------------
    settled = evaluate(tc, s["exam_id"])
    assert settled, "incidents must still be projected"
    assert {inc["status"] for inc in settled} == {"RESOLVED"}
    assert {inc["severity"] for inc in settled} == {"CANDIDATE", "NODE", "SYSTEM"}
    assert all(inc["resolved_at"] is not None for inc in settled)
    assert all(inc["root_cause_summary"] for inc in settled)
    # The SYSTEM incident resolves on its own terms once every session is back.
    settled_system = [inc for inc in settled if inc["severity"] == "SYSTEM"]
    assert len(settled_system) == 1
    assert settled_system[0]["root_cause_summary"].startswith("System recovered:")
    snap = overview(tc, s["exam_id"])
    assert {row["status"] for row in snap["incidents"]} == {"RESOLVED"}

    # --- 7. the whole global ledger verifies ------------------------------
    audit = tc.post("/audit/verify").json()
    assert audit["valid"] is True
    assert audit["first_broken_sequence_no"] is None
    assert audit["events_checked"] == snap["latest_event_sequence_no"]
    assert audit["events_checked"] > 0
    assert snap["audit_status"]["valid"] is True

    # --- 8. evidence package for a resolved incident ---------------------
    resolved_incident = settled[0]
    ev = tc.get(f"/incident/{resolved_incident['id']}/evidence")
    assert ev.status_code == 200, ev.text
    package = ev.json()
    assert package["audit_status"]["valid"] is True
    assert package["evidence_events"]
    assert package["recovery_facts"]["node_failure_count"] >= 1
    assert package["recovery_facts"]["node_recovery_count"] >= 1
    assert package["recovery_facts"]["session_recovered_event_count"] >= 1
    assert [e["sequence_no"] for e in package["evidence_events"]] == sorted(
        e["sequence_no"] for e in package["evidence_events"]
    )

    # --- 9. mocked Gemini analysis stays grounded -------------------------
    grounded_ref = package["evidence_events"][0]["sequence_no"]
    mock_gemini(monkeypatch, [grounded_ref])
    analysis = tc.post(f"/incident/{resolved_incident['id']}/analyze")
    assert analysis.status_code == 200, analysis.text
    body = analysis.json()
    assert body["incident_id"] == resolved_incident["id"]
    assert body["evidence_refs"] == [grounded_ref]
    assert body["likely_cause"]
    assert body["impact_summary"]
    assert body["recommended_response"]

    # --- 10. the final overview reflects every transition -----------------
    final = overview(tc, s["exam_id"])
    assert final["nodes"][0]["status"] == "HEALTHY"
    assert {row["status"] for row in final["sessions"]} == {"ACTIVE"}
    assert {row["status"] for row in final["incidents"]} == {"RESOLVED"}
    assert final["audit_status"]["valid"] is True
    assert final["latest_event_sequence_no"] == audit["events_checked"]
    assert final["latest_event_sequence_no"] == count(factory, Event)


def test_walkthrough_ledger_records_the_real_event_history(client):
    """Every step of the walkthrough is a genuine, typed ledger event."""
    tc, factory = client
    s = create_scenario(tc)
    sessions, _, _ = run_to_failure(tc, s)
    recover_node(tc, s["node_id"])
    for index, session in enumerate(sessions):
        reconcile(
            tc,
            session["session_id"],
            [
                {
                    "client_event_id": f"buf-{index}",
                    "question_id": s["question_ids"][0],
                    "answer": "B",
                }
            ],
        )
    evaluate(tc, s["exam_id"])

    types = event_types(factory)
    assert types.count("SESSION_STARTED") == 3
    assert types.count("ANSWER_SAVED") == 6  # 3 live + 3 buffered
    assert types.count("NODE_FAILURE_INJECTED") == 1
    assert types.count("NODE_RECOVERY_INITIATED") == 1
    assert types.count("SESSION_RECOVERED") == 3
    # The incident engine never appends events of its own.
    assert "INCIDENT_OPENED" not in types
    assert count(factory, Event) == len(types)
    assert tc.post("/audit/verify").json()["valid"] is True


def test_walkthrough_tamper_then_overview_reports_broken_chain(client):
    """The demo tamper switch is reflected in the overview audit status."""
    tc, _ = client
    s = create_scenario(tc)
    run_to_failure(tc, s)

    assert overview(tc, s["exam_id"])["audit_status"]["valid"] is True

    tamper = tc.post("/demo/tamper", json={"target": "latest", "field": "payload"})
    assert tamper.status_code == 200, tamper.text

    snap = overview(tc, s["exam_id"])
    assert snap["audit_status"]["valid"] is False
    assert snap["audit_status"]["first_broken_sequence_no"] == (
        tamper.json()["tampered_sequence_no"]
    )
    assert tc.post("/audit/verify").json()["valid"] is False

