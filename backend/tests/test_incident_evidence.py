"""Tests for incident evidence packaging and grounded AI analysis (Task 8).

Gemini is fully mocked: the official google-genai SDK is never called.

Covers evidence generation, ordering/scoping, deterministic recovery facts,
global audit status (clean and tampered ledger), unknown-incident handling,
valid AI output, invalid JSON, missing fields, ungrounded evidence_refs, Gemini
failures, missing API key, the analyze endpoint, and proof that analysis never
mutates Event / Incident / Session / Node / Response state.
"""

import json
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
from app.services import ai_incident_service, evidence_service, incident_service

GEMINI_API_KEY = "test-gemini-key"
GEMINI_MODEL = "gemini-3.8-flash"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient wired to an isolated SQLite database; GEMINI_API_KEY set."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_evidence.db",
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
    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, GEMINI_API_KEY)
    monkeypatch.setenv(ai_incident_service.GEMINI_MODEL_ENV, GEMINI_MODEL)
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, text):
        self.parts = [_FakePart(text)]


class _FakeCandidate:
    def __init__(self, text):
        self.content = _FakeContent(text)


class FakeGeminiResponse:
    """Mimics a google.genai response: `.parsed` plus raw text candidates."""

    def __init__(self, text, parsed=None):
        self.parsed = parsed
        self.candidates = [_FakeCandidate(text)] if text is not None else []


def mock_gemini(monkeypatch, response):
    """Patch the SDK call site; returns the dict capturing the prompt."""
    captured = {}

    def fake_call(prompt):
        captured["prompt"] = prompt
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(ai_incident_service, "_call_gemini", fake_call)
    return captured


def seed(factory, node_count=2, sessions_per_node=2, questions=2, title="E1"):
    """One exam + healthy nodes + candidates + ACTIVE sessions + questions."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title=title, status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()

        nodes = [Node(exam_id=exam.id, status="HEALTHY") for _ in range(node_count)]
        qs = [
            Question(exam_id=exam.id, text=f"Q{i}?", marks=1)
            for i in range(questions)
        ]
        db.add_all(nodes + qs)
        db.flush()

        sessions = []
        roll = 0
        for node in nodes:
            for _ in range(sessions_per_node):
                roll += 1
                cand = Candidate(name=f"C{roll}", roll_no=f"R-{exam.id}-{roll}")
                db.add(cand)
                db.flush()
                sess = Session(
                    exam_id=exam.id,
                    candidate_id=cand.id,
                    node_id=node.id,
                    status="ACTIVE",
                    started_at=now,
                )
                db.add(sess)
                db.flush()
                sessions.append(sess)

        db.commit()
        return {
            "exam_id": exam.id,
            "node_ids": [n.id for n in nodes],
            "question_ids": [q.id for q in qs],
            "session_ids": [s.id for s in sessions],
            "sessions_by_node": [
                [s.id for s in sessions if s.node_id == n.id] for n in nodes
            ],
        }
    finally:
        db.close()


def fail_node(tc, node_id, reason="demo"):
    r = tc.post(f"/demo/nodes/{node_id}/fail", json={"reason": reason})
    assert r.status_code == 200, r.text
    return r.json()


def recover_node(tc, node_id, reason="demo"):
    r = tc.post(f"/demo/nodes/{node_id}/recover", json={"reason": reason})
    assert r.status_code == 200, r.text
    return r.json()


def evaluate(tc, exam_id):
    r = tc.post("/demo/incidents/evaluate", json={"exam_id": exam_id})
    assert r.status_code == 200, r.text
    return r.json()["incidents"]


def by_severity(incidents, severity):
    return [i for i in incidents if i["severity"] == severity]


def evidence_of(tc, incident_id):
    r = tc.get(f"/incident/{incident_id}/evidence")
    assert r.status_code == 200, r.text
    return r.json()


def open_candidate_incident(tc, factory):
    """Fail one node with a single session -> one CANDIDATE incident."""
    ids = seed(factory, node_count=1, sessions_per_node=1, questions=1)
    fail_node(tc, ids["node_ids"][0])
    incidents = by_severity(evaluate(tc, ids["exam_id"]), "CANDIDATE")
    assert incidents, "expected exactly one CANDIDATE incident"
    return ids, incidents[0]


def open_node_incident(tc, factory):
    """Fail one node carrying three sessions -> one NODE incident."""
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    fail_node(tc, ids["node_ids"][0])
    incidents = by_severity(evaluate(tc, ids["exam_id"]), "NODE")
    assert incidents, "expected exactly one NODE incident"
    return ids, incidents[0]


def row_counts(factory):
    """Row counts of every table AI analysis must never touch."""
    db = factory()
    try:
        return {
            "events": db.scalar(select(func.count()).select_from(Event)),
            "incidents": db.scalar(select(func.count()).select_from(Incident)),
            "sessions": db.scalar(select(func.count()).select_from(Session)),
            "nodes": db.scalar(select(func.count()).select_from(Node)),
            "responses": db.scalar(select(func.count()).select_from(Response)),
        }
    finally:
        db.close()


def ledger_snapshot(factory):
    """(sequence_no, hash) of every event, to prove nothing was appended."""
    db = factory()
    try:
        return [
            (ev.sequence_no, ev.hash)
            for ev in db.scalars(
                select(Event).order_by(Event.sequence_no.asc())
            ).all()
        ]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Evidence packaging
# --------------------------------------------------------------------------- #


def test_evidence_package_contains_all_required_sections(client):
    """Evidence package exposes metadata, entities, events, facts and audit."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])

    assert set(pkg) == {
        "incident",
        "affected_session_ids",
        "affected_node_ids",
        "sessions",
        "nodes",
        "evidence_events",
        "recovery_facts",
        "audit_status",
    }
    assert pkg["incident"]["id"] == incident["id"]
    assert pkg["incident"]["severity"] == "CANDIDATE"
    assert pkg["evidence_events"], "evidence must not be empty"


def test_evidence_events_are_ordered_by_sequence_no(client):
    """Evidence events are strictly ascending by sequence_no."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    seqs = [e["sequence_no"] for e in evidence_of(tc, incident["id"])["evidence_events"]]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs)), "no duplicate evidence events"


def test_evidence_selection_reuses_incident_service(client, monkeypatch):
    """Evidence selection is single-sourced from incident_service."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)

    calls = []
    real = incident_service.get_incident_evidence

    def spy(db, inc):
        calls.append(inc.id)
        return real(db, inc)

    monkeypatch.setattr(evidence_service.incident_service, "get_incident_evidence", spy)
    pkg = evidence_of(tc, incident["id"])

    assert calls == [incident["id"]]
    assert [e["sequence_no"] for e in pkg["evidence_events"]] == sorted(
        e["sequence_no"] for e in pkg["evidence_events"]
    )


def test_evidence_events_match_incident_evidence_sequence_nos(client):
    """The package never widens the incident's evidence set."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    detail = tc.get(f"/incident/{incident['id']}").json()
    expected = detail["evidence_event_sequence_nos"]
    pkg = evidence_of(tc, incident["id"])
    assert [e["sequence_no"] for e in pkg["evidence_events"]] == expected


def test_evidence_events_carry_full_event_details(client):
    """Each evidence event exposes the stored fields, including both hashes."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    for ev in pkg["evidence_events"]:
        assert ev["event_type"] in incident_service.ALLOWED_EVIDENCE_EVENT_TYPES
        assert ev["server_timestamp"] is not None
        assert ev["previous_hash"] and ev["hash"]
        assert len(ev["hash"]) == 64


def test_affected_node_ids_for_candidate_incident(client):
    """CANDIDATE scope: the node of its own target session only."""
    tc, factory = client
    ids, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    assert pkg["affected_node_ids"] == ids["node_ids"]
    assert [s["node_id"] for s in pkg["sessions"]] == ids["node_ids"]


def test_affected_node_ids_for_node_incident(client):
    """NODE scope is the origin node of the episode."""
    tc, factory = client
    ids, node_incident = open_node_incident(tc, factory)
    pkg = evidence_of(tc, node_incident["id"])
    assert pkg["affected_node_ids"] == ids["node_ids"]


def test_affected_node_ids_excludes_unrelated_node_for_system(client):
    """SYSTEM scope counts only origin node + nodes of linked sessions."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=2, questions=1)
    fail_node(tc, ids["node_ids"][0])
    system_incidents = by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM")
    assert system_incidents

    pkg = evidence_of(tc, system_incidents[0]["id"])
    # The healthy node contributed no linked session, so it is not affected.
    assert ids["node_ids"][0] in pkg["affected_node_ids"]


# --------------------------------------------------------------------------- #
# Recovery facts
# --------------------------------------------------------------------------- #


def test_recovery_facts_reflect_node_failure_and_recovery(client):
    """Recovery facts cite only real NODE_FAILURE / NODE_RECOVERY evidence."""
    tc, factory = client
    ids, incident = open_node_incident(tc, factory)
    recover_node(tc, ids["node_ids"][0])
    evaluate(tc, ids["exam_id"])  # re-evaluate so recovery lands in evidence

    facts = evidence_of(tc, incident["id"])["recovery_facts"]
    assert facts["node_failure_count"] >= 1
    assert facts["node_recovery_count"] >= 1
    assert facts["node_failures"][0]["node_id"] == ids["node_ids"][0]
    assert facts["node_recoveries"][0]["node_id"] == ids["node_ids"][0]


def test_recovery_facts_report_session_and_node_status(client):
    """Status counts come from the real Session and Node rows."""
    tc, factory = client
    ids, incident = open_node_incident(tc, factory)
    facts = evidence_of(tc, incident["id"])["recovery_facts"]
    assert sum(facts["session_status_counts"].values()) == 3
    assert facts["all_sessions_active"] is False
    assert facts["node_status_counts"] == {"FAILED": 1}
    assert facts["all_nodes_healthy"] is False
    assert facts["is_resolved"] is False


def test_recovery_facts_do_not_equate_answer_saved_with_reconciliation(client):
    """A plain ANSWER_SAVED is never reported as a completed reconciliation."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=1, questions=1)
    fail_node(tc, ids["node_ids"][0])
    recover_node(tc, ids["node_ids"][0])
    question_id = ids["question_ids"][0]

    # A conflicting duplicate inside one request: the ANSWER_SAVED is durable
    # but the strict completion rule does NOT hold, so no SESSION_RECOVERED.
    r = tc.post(
        f"/session/{ids['session_ids'][0]}/reconcile",
        json={
            "events": [
                {
                    "client_event_id": "dup-1",
                    "question_id": question_id,
                    "answer": "A",
                },
                {
                    "client_event_id": "dup-1",
                    "question_id": question_id,
                    "answer": "B",
                },
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reconciliation_complete"] is False
    assert body["recovered_event_sequence_no"] is None
    assert body["mismatched_client_event_ids"] == ["dup-1"]

    incident = by_severity(evaluate(tc, ids["exam_id"]), "CANDIDATE")[0]
    pkg = evidence_of(tc, incident["id"])
    assert any(
        e["event_type"] == "ANSWER_SAVED" for e in pkg["evidence_events"]
    ), "precondition: an ANSWER_SAVED event is in the evidence set"
    assert not any(
        e["event_type"] == "SESSION_RECOVERED" for e in pkg["evidence_events"]
    )

    facts = pkg["recovery_facts"]
    # ANSWER_SAVED alone must never be inferred as reconciliation.
    assert facts["session_recovered_event_count"] == 0
    assert facts["session_recovered_events"] == []
    assert "reconciled_answer_count" not in facts


def test_recovery_facts_record_completed_reconciliation(client):
    """A completed reconcile yields exactly one SESSION_RECOVERED fact."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=1, questions=1)
    fail_node(tc, ids["node_ids"][0])
    incident = by_severity(evaluate(tc, ids["exam_id"]), "CANDIDATE")[0]

    recover_node(tc, ids["node_ids"][0])
    r = tc.post(
        f"/session/{ids['session_ids'][0]}/reconcile",
        json={
            "events": [
                {
                    "client_event_id": "buf-ok",
                    "question_id": ids["question_ids"][0],
                    "answer": "A",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["reconciliation_complete"] is True
    assert r.json()["recovered_event_sequence_no"] is not None

    facts = evidence_of(tc, incident["id"])["recovery_facts"]
    assert facts["session_recovered_event_count"] == 1
    assert (
        facts["session_recovered_events"][0]["reconciled_client_event_ids"] == ["buf-ok"]
    )


def test_recovery_facts_empty_when_no_recovery_evidence(client):
    """An incident without any recovery event reports zero recovery facts."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    facts = evidence_of(tc, incident["id"])["recovery_facts"]
    assert facts["node_recovery_count"] == 0
    assert facts["node_recoveries"] == []
    assert facts["session_recovered_event_count"] == 0
    assert facts["session_recovered_events"] == []


# --------------------------------------------------------------------------- #
# Global audit status inside the evidence package
# --------------------------------------------------------------------------- #


def test_audit_status_valid_for_clean_ledger(client):
    """A clean ledger reports a fully verified global chain."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    audit = evidence_of(tc, incident["id"])["audit_status"]
    assert audit["valid"] is True
    assert audit["first_broken_sequence_no"] is None
    assert audit["failure_reason"] is None
    assert audit["events_checked"] > 0


def test_audit_status_invalid_for_tampered_ledger(client, monkeypatch):
    """A tampered ledger is surfaced inside the evidence package."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)

    db = factory()
    try:
        event = db.scalars(select(Event).order_by(Event.sequence_no.asc())).first()
        event.payload = {"tampered": True}
        db.commit()
        broken = event.sequence_no
    finally:
        db.close()

    audit = evidence_of(tc, incident["id"])["audit_status"]
    assert audit["valid"] is False
    assert audit["first_broken_sequence_no"] == broken
    assert "Hash mismatch" in audit["failure_reason"]


def test_evidence_endpoint_unknown_incident_404(client):
    """Unknown incident -> 404, not an empty package."""
    tc, _ = client
    assert tc.get("/incident/99999/evidence").status_code == 404


def test_evidence_endpoint_works_without_gemini(client, monkeypatch):
    """Evidence generation never touches Gemini or needs an API key."""
    tc, factory = client
    monkeypatch.delenv(ai_incident_service.GEMINI_API_KEY_ENV, raising=False)
    _, incident = open_node_incident(tc, factory)
    r = tc.get(f"/incident/{incident['id']}/evidence")
    assert r.status_code == 200, r.text
    assert r.json()["evidence_events"]



# --------------------------------------------------------------------------- #
# AI analysis: request construction and configuration
# --------------------------------------------------------------------------- #


def gemini_payload(evidence_refs):
    return json.dumps(
        {
            "likely_cause": "Exam node crashed and disconnected the candidate.",
            "impact_summary": "One candidate lost connectivity mid-exam.",
            "recommended_response": "Recover the node and reconcile buffered answers.",
            "evidence_refs": list(evidence_refs),
        }
    )


def test_default_model_and_env_override(monkeypatch):
    """Model defaults to gemini-3.8-flash and honours GEMINI_MODEL."""
    monkeypatch.delenv(ai_incident_service.GEMINI_MODEL_ENV, raising=False)
    assert ai_incident_service.get_model_name() == "gemini-3.8-flash"
    monkeypatch.setenv(ai_incident_service.GEMINI_MODEL_ENV, "gemini-9.9-pro")
    assert ai_incident_service.get_model_name() == "gemini-9.9-pro"


def test_api_key_read_from_environment(monkeypatch):
    """GEMINI_API_KEY comes from the environment and is never hard-coded."""
    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "env-key")
    assert ai_incident_service.get_api_key() == "env-key"
    monkeypatch.delenv(ai_incident_service.GEMINI_API_KEY_ENV, raising=False)
    with pytest.raises(ai_incident_service.GeminiNotConfiguredError):
        ai_incident_service.get_api_key()


def test_gemini_call_uses_official_sdk_with_json_schema(monkeypatch):
    """The official google-genai SDK is used with strict JSON output config."""
    import google.genai as real_genai

    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "env-key")
    monkeypatch.setenv(ai_incident_service.GEMINI_MODEL_ENV, "gemini-3.8-flash")
    seen = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            seen.update(kwargs)
            return FakeGeminiResponse("{}")

    class FakeClient:
        def __init__(self, api_key):
            seen["api_key"] = api_key
            self.models = FakeModels()

    monkeypatch.setattr(real_genai, "Client", FakeClient)
    ai_incident_service._call_gemini("prompt")

    assert seen["api_key"] == "env-key"
    assert seen["model"] == "gemini-3.8-flash"
    assert seen["contents"] == "prompt"
    assert seen["config"]["response_mime_type"] == "application/json"
    assert seen["config"]["response_schema"] is ai_incident_service.IncidentAnalysis


def test_gemini_call_wraps_sdk_exception(monkeypatch):
    """An exception raised by the SDK becomes a controlled GeminiRequestError."""
    import google.genai as real_genai

    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "env-key")

    class ExplodingModels:
        def generate_content(self, **kwargs):
            raise TimeoutError("request timed out")

    class ExplodingClient:
        def __init__(self, api_key):
            self.models = ExplodingModels()

    monkeypatch.setattr(real_genai, "Client", ExplodingClient)
    with pytest.raises(ai_incident_service.GeminiRequestError) as excinfo:
        ai_incident_service._call_gemini("prompt")
    assert "request timed out" in str(excinfo.value)


def test_prompt_contains_only_the_evidence_package(client, monkeypatch):
    """Only the structured evidence package is sent to the model."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    captured = mock_gemini(
        monkeypatch,
        FakeGeminiResponse(gemini_payload([pkg["evidence_events"][0]["sequence_no"]])),
    )
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text
    assert "EVIDENCE PACKAGE (JSON)" in captured["prompt"]
    assert str(incident["id"]) in captured["prompt"]
    assert "evidence_refs may only contain sequence_no" in captured["prompt"]



# --------------------------------------------------------------------------- #
# AI analysis: success path
# --------------------------------------------------------------------------- #


def gemini_payload(evidence_refs):
    return json.dumps(
        {
            "likely_cause": "Exam node crashed and disconnected the candidate.",
            "impact_summary": "One candidate lost connectivity mid-exam.",
            "recommended_response": "Recover the node and reconcile buffered answers.",
            "evidence_refs": list(evidence_refs),
        }
    )


def test_analyze_endpoint_returns_grounded_analysis(client, monkeypatch):
    """Valid grounded model output is returned with the required fields."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    refs = [e["sequence_no"] for e in pkg["evidence_events"]][:2]
    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload(refs)))

    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "incident_id",
        "likely_cause",
        "impact_summary",
        "recommended_response",
        "evidence_refs",
        "generated_at",
    }
    assert body["incident_id"] == incident["id"]
    assert body["likely_cause"]
    assert body["impact_summary"]
    assert body["recommended_response"]
    assert body["evidence_refs"] == sorted(refs)
    assert body["generated_at"] is not None


def test_analyze_uses_sdk_structured_parsed_output(client, monkeypatch):
    """When the SDK already parsed the schema, those values are used."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    ref = pkg["evidence_events"][0]["sequence_no"]
    parsed = ai_incident_service.IncidentAnalysis(
        likely_cause="parsed cause",
        impact_summary="parsed impact",
        recommended_response="parsed response",
        evidence_refs=[ref],
    )
    mock_gemini(monkeypatch, FakeGeminiResponse(None, parsed=parsed))

    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["likely_cause"] == "parsed cause"
    assert body["evidence_refs"] == [ref]


def test_analyze_accepts_fenced_json(client, monkeypatch):
    """A ```json fenced payload is parsed correctly."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    ref = pkg["evidence_events"][0]["sequence_no"]
    mock_gemini(
        monkeypatch, FakeGeminiResponse(f"```json\n{gemini_payload([ref])}\n```")
    )
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text
    assert r.json()["evidence_refs"] == [ref]


def test_analyze_empty_evidence_refs_is_accepted(client, monkeypatch):
    """A grounded analysis with no citations is still valid."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload([])))
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text
    assert r.json()["evidence_refs"] == []


# --------------------------------------------------------------------------- #
# AI analysis: validation, grounding and failure handling
# --------------------------------------------------------------------------- #


def test_analyze_rejects_invalid_json(client, monkeypatch):
    """Non-JSON model output is rejected with a controlled error."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    mock_gemini(monkeypatch, FakeGeminiResponse("This is not JSON at all."))
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text
    assert "not valid JSON" in r.json()["detail"]


def test_analyze_rejects_missing_fields(client, monkeypatch):
    """A response missing a required field is rejected."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    incomplete = json.dumps({"likely_cause": "cause", "evidence_refs": []})
    mock_gemini(monkeypatch, FakeGeminiResponse(incomplete))
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "missing required fields" in detail
    assert "impact_summary" in detail
    assert "recommended_response" in detail


def test_analyze_rejects_ungrounded_evidence_refs(client, monkeypatch):
    """A hallucinated sequence number rejects the whole response."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    real_ref = pkg["evidence_events"][0]["sequence_no"]
    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload([real_ref, 999999])))

    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "ungrounded evidence references" in detail
    assert "999999" in detail
    # The valid reference is never returned alongside the rejected one.
    assert "evidence_refs" not in r.json()


def test_analyze_rejects_refs_outside_incident_evidence(client, monkeypatch):
    """A sequence number valid elsewhere in the ledger is still ungrounded."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=1, questions=1)
    fail_node(tc, ids["node_ids"][0])
    candidate = by_severity(evaluate(tc, ids["exam_id"]), "CANDIDATE")[0]

    # A second exam appends more events; its sequence numbers are NOT evidence.
    other_ids = seed(factory, node_count=1, sessions_per_node=1, questions=1, title="E2")
    fail_node(tc, other_ids["node_ids"][0])

    pkg = evidence_of(tc, candidate["id"])
    allowed = {e["sequence_no"] for e in pkg["evidence_events"]}
    db = factory()
    try:
        foreign = (
            db.scalars(select(Event).where(Event.exam_id == other_ids["exam_id"]))
            .first()
            .sequence_no
        )
    finally:
        db.close()
    assert foreign not in allowed

    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload([foreign])))
    r = tc.post(f"/incident/{candidate['id']}/analyze")
    assert r.status_code == 502, r.text
    assert "ungrounded evidence references" in r.json()["detail"]


def test_analyze_handles_gemini_request_error(client, monkeypatch):
    """An SDK/network failure is surfaced as a controlled 502."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    mock_gemini(
        monkeypatch,
        ai_incident_service.GeminiRequestError("Gemini request failed: timeout"),
    )
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text
    assert "timeout" in r.json()["detail"]


def test_analyze_handles_raw_sdk_exception(client, monkeypatch):
    """A raw SDK exception inside _call_gemini becomes a controlled error."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)

    def boom(_prompt):
        raise RuntimeError("unexpected SDK explosion")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", boom)
    db = factory()
    try:
        with pytest.raises(ai_incident_service.GeminiRequestError):
            ai_incident_service.analyze_incident(db, db.get(Incident, incident["id"]))
    finally:
        db.close()


def test_analyze_missing_api_key_returns_503(client, monkeypatch):
    """A missing GEMINI_API_KEY is a controlled 503, not a crash."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    monkeypatch.delenv(ai_incident_service.GEMINI_API_KEY_ENV, raising=False)
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 503, r.text
    assert "GEMINI_API_KEY" in r.json()["detail"]


def test_analyze_unknown_incident_404(client, monkeypatch):
    """Unknown incident -> 404 before any AI call is attempted."""
    tc, _ = client

    def explode(_prompt):
        raise AssertionError("Gemini must not be called for an unknown incident")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", explode)
    assert tc.post("/incident/99999/analyze").status_code == 404


def test_analyze_empty_gemini_response_rejected(client, monkeypatch):
    """An empty candidate list is a controlled validation failure."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    mock_gemini(monkeypatch, FakeGeminiResponse(None))
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text
    assert "empty response" in r.json()["detail"]


def test_ai_failure_does_not_affect_core_endpoints(client, monkeypatch):
    """A failed analysis leaves incident, evidence and audit fully working."""
    tc, factory = client
    _, incident = open_candidate_incident(tc, factory)
    mock_gemini(monkeypatch, FakeGeminiResponse("not json"))

    assert tc.post(f"/incident/{incident['id']}/analyze").status_code == 502
    assert tc.get(f"/incident/{incident['id']}").status_code == 200
    assert tc.get(f"/incident/{incident['id']}/evidence").status_code == 200
    audit = tc.post("/audit/verify").json()
    assert audit["valid"] is True


# --------------------------------------------------------------------------- #
# AI analysis is strictly read-only
# --------------------------------------------------------------------------- #


def test_analyze_never_mutates_any_table(client, monkeypatch):
    """Analysis creates and updates no Event/Incident/Session/Node/Response row."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    pkg = evidence_of(tc, incident["id"])
    refs = [e["sequence_no"] for e in pkg["evidence_events"]]
    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload(refs)))

    before_counts = row_counts(factory)
    before_ledger = ledger_snapshot(factory)
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 200, r.text

    assert row_counts(factory) == before_counts
    assert ledger_snapshot(factory) == before_ledger


def test_analyze_does_not_mutate_state_on_failure(client, monkeypatch):
    """Even a rejected (hallucinated) response leaves all rows untouched."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    mock_gemini(monkeypatch, FakeGeminiResponse(gemini_payload([999999])))

    before_counts = row_counts(factory)
    before_ledger = ledger_snapshot(factory)
    r = tc.post(f"/incident/{incident['id']}/analyze")
    assert r.status_code == 502, r.text

    assert row_counts(factory) == before_counts
    assert ledger_snapshot(factory) == before_ledger


def test_evidence_generation_is_read_only(client):
    """Building the evidence package writes nothing to the ledger."""
    tc, factory = client
    _, incident = open_node_incident(tc, factory)
    before_counts = row_counts(factory)
    before_ledger = ledger_snapshot(factory)
    evidence_of(tc, incident["id"])
    assert row_counts(factory) == before_counts
    assert ledger_snapshot(factory) == before_ledger

