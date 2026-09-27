"""Backend hardening + frontend integration tests (Task 10).

Covers CORS behaviour, schema default isolation, the health endpoint,
representative 404/409/503 contract mappings, demo overview availability, and
controlled AI provider failure behaviour. Also asserts the generated OpenAPI
document still describes every current endpoint and schema.

No domain semantics are exercised here: this suite is about the HTTP contract.
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.schemas import (
    HealthResponse,
    IncidentResponse,
    ReconcileRequest,
    ReconcileResponse,
)
from app.core.cors import (
    DEFAULT_ALLOW_ORIGINS,
    get_allow_credentials,
    parse_origins,
)
from app.core.database import get_db
from app.db import Base, Candidate, Exam, Node, Question
from app.main import app
from app.services import ai_incident_service

VITE_ORIGIN = "http://localhost:5173"
CRA_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "https://evil.example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient on an isolated DB; CORS defaults and a Gemini key set."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_hardening.db",
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


def seed_exam(factory, sessions=1):
    """One exam, one HEALTHY node, candidates, questions and ACTIVE sessions."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title="H", status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        node = Node(exam_id=exam.id, status="HEALTHY")
        db.add(node)
        db.flush()
        question = Question(exam_id=exam.id, text="Q?", marks=1)
        db.add(question)
        db.flush()
        made = []
        for i in range(sessions):
            candidate = Candidate(name=f"C{i}", roll_no=f"H-{exam.id}-{i}")
            db.add(candidate)
            db.flush()
            made.append(candidate.id)
        db.commit()
        return {
            "exam_id": exam.id,
            "node_id": node.id,
            "candidate_ids": made,
            "question_ids": [question.id],
        }
    finally:
        db.close()


def _set_node_status(factory, node_id: int, status: str) -> None:
    """Put a node into an arbitrary status (e.g. DEGRADED) for state tests."""
    db = factory()
    try:
        node = db.get(Node, node_id)
        node.status = status
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #


def test_default_origins_cover_both_local_frontend_ports(monkeypatch):
    """Unset env -> the two normal local frontend dev origins."""
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    assert parse_origins(None) == list(DEFAULT_ALLOW_ORIGINS)
    assert CRA_ORIGIN in DEFAULT_ALLOW_ORIGINS
    assert VITE_ORIGIN in DEFAULT_ALLOW_ORIGINS


def test_origins_are_parsed_from_env():
    """CORS_ALLOW_ORIGINS is a comma-separated list, whitespace tolerated."""
    raw = f"{CRA_ORIGIN}, {VITE_ORIGIN} ,"
    assert parse_origins(raw) == [CRA_ORIGIN, VITE_ORIGIN]


def test_blank_or_unparsable_origins_fall_back_to_defaults():
    """An empty variable never silently opens the API up."""
    assert parse_origins("") == list(DEFAULT_ALLOW_ORIGINS)
    assert parse_origins("   ,  ,") == list(DEFAULT_ALLOW_ORIGINS)
    assert parse_origins(None) == list(DEFAULT_ALLOW_ORIGINS)


def test_trailing_slashes_are_normalised():
    """A trailing slash must not create a duplicate origin entry."""
    assert parse_origins(f"{CRA_ORIGIN}/") == [CRA_ORIGIN]


def test_credentials_never_combined_with_wildcard():
    """Wildcard + credentials is a browser-rejected policy; we refuse it."""
    assert get_allow_credentials(["*"]) is False
    assert get_allow_credentials(list(DEFAULT_ALLOW_ORIGINS)) is True


def test_cors_allows_configured_local_frontend(client):
    """A permitted origin receives the CORS headers on a real request."""
    tc, _ = client
    r = tc.get("/health", headers={"Origin": CRA_ORIGIN})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == CRA_ORIGIN


def test_cors_allows_vite_frontend(client):
    tc, _ = client
    r = tc.get("/health", headers={"Origin": VITE_ORIGIN})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == VITE_ORIGIN


def test_cors_preflight_succeeds_for_configured_origin(client):
    """A browser preflight for the local frontend is answered."""
    tc, _ = client
    r = tc.options(
        "/session/start",
        headers={
            "Origin": VITE_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == VITE_ORIGIN
    assert "POST" in r.headers.get("access-control-allow-methods", "")


def test_cors_rejects_unknown_origin(client):
    """An unlisted origin gets no allow-origin header."""
    tc, _ = client
    r = tc.get("/health", headers={"Origin": EVIL_ORIGIN})
    assert r.status_code == 200  # request still served
    assert "access-control-allow-origin" not in r.headers


def test_cors_never_advertises_wildcard_origin(client):
    """No wildcard origin is reflected back to the browser."""
    tc, _ = client
    r = tc.get("/health", headers={"Origin": CRA_ORIGIN})
    assert r.headers.get("access-control-allow-origin") != "*"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_health_returns_ok(client):
    """/health stays a minimal liveness probe."""
    tc, _ = client
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_needs_no_database_state(client):
    """Health answers on a completely empty database, repeatably."""
    tc, _ = client
    assert tc.get("/health").json() == {"status": "ok"}
    assert tc.get("/health").json() == {"status": "ok"}



# --------------------------------------------------------------------------- #
# Schema default isolation
# --------------------------------------------------------------------------- #


def _reconcile_response(session_id: int) -> ReconcileResponse:
    return ReconcileResponse(
        session_id=session_id,
        status="RECOVERING",
        submitted_client_event_ids=[],
        acknowledged_client_event_ids=[],
        newly_reconciled_client_event_ids=[],
        already_acknowledged_client_event_ids=[],
        missing_client_event_ids=[],
        mismatched_client_event_ids=[],
        rejected_client_event_ids=[],
        reconciliation_complete=False,
    )


def test_reconcile_request_default_is_not_shared():
    """Two default ReconcileRequests must not share one list object."""
    first, second = ReconcileRequest(), ReconcileRequest()
    assert first.events == [] and second.events == []
    first.events.append("x")
    assert second.events == [], "default list leaked between instances"


def test_reconcile_response_dict_default_is_not_shared():
    """The rejected_reasons default must be a fresh dict each time."""
    first, second = _reconcile_response(1), _reconcile_response(2)
    assert first.rejected_reasons == {} and second.rejected_reasons == {}
    first.rejected_reasons["k"] = "reason"
    assert second.rejected_reasons == {}, "default dict leaked between instances"


def test_incident_response_list_defaults_are_not_shared():
    """IncidentResponse list defaults must be independent per instance."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    first = IncidentResponse(
        id=1, exam_id=1, severity="CANDIDATE", status="ACTIVE", detected_at=now
    )
    second = IncidentResponse(
        id=2, exam_id=1, severity="NODE", status="ACTIVE", detected_at=now
    )
    assert first.affected_session_ids == []
    assert first.evidence_event_sequence_nos == []
    first.affected_session_ids.append(7)
    first.evidence_event_sequence_nos.append(7)
    assert second.affected_session_ids == []
    assert second.evidence_event_sequence_nos == []


def test_no_schema_declares_a_bare_mutable_default():
    """Guard: no Pydantic model may default to a shared [] or {}."""
    from app.api import schemas

    offenders = []
    for name in dir(schemas):
        model = getattr(schemas, name)
        if not (isinstance(model, type) and hasattr(model, "model_fields")):
            continue
        for field_name, field in model.model_fields.items():
            default = field.get_default(call_default_factory=False)
            if isinstance(default, (list, dict, set)):
                offenders.append(f"{name}.{field_name}")
    assert offenders == []



# --------------------------------------------------------------------------- #
# HTTP contract: 404 / 409 / 503
# --------------------------------------------------------------------------- #


def test_unknown_session_state_404(client):
    tc, _ = client
    assert tc.get("/session/99999/state").status_code == 404


def test_unknown_incident_404(client):
    tc, _ = client
    assert tc.get("/incident/99999").status_code == 404
    assert tc.get("/incident/99999/evidence").status_code == 404
    assert tc.post("/incident/99999/analyze").status_code == 404


def test_unknown_node_fail_and_recover_404(client):
    tc, _ = client
    assert tc.post("/demo/nodes/99999/fail", json={}).status_code == 404
    assert tc.post("/demo/nodes/99999/recover", json={}).status_code == 404


def test_unknown_exam_evaluate_404(client):
    tc, _ = client
    r = tc.post("/demo/incidents/evaluate", json={"exam_id": 99999})
    assert r.status_code == 404


def test_unknown_exam_overview_404(client):
    tc, _ = client
    r = tc.get("/demo/overview/99999")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def _start_one_session(tc, ids):
    r = tc.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_ids"][0],
            "node_id": ids["node_id"],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_invalid_state_answer_on_recovering_is_409(client):
    """Answering while RECOVERING is a state conflict, not an outage."""
    tc, factory = client
    ids = seed_exam(factory, sessions=1)
    session = _start_one_session(tc, ids)
    tc.post(f"/demo/nodes/{ids['node_id']}/fail", json={})
    tc.post(f"/demo/nodes/{ids['node_id']}/recover", json={})

    r = tc.post(
        f"/session/{session['session_id']}/answer",
        json={
            "question_id": ids["question_ids"][0],
            "answer": "A",
            "client_event_id": "conflict-1",
        },
    )
    assert r.status_code == 409


def test_invalid_state_recover_degraded_node_is_409(client):
    """A node in a non-recoverable state is a state conflict.

    Only FAILED is recoverable and a HEALTHY node is deliberately idempotent,
    so DEGRADED is the representative 409 for the recovery transition.
    """
    tc, factory = client
    ids = seed_exam(factory, sessions=1)
    _set_node_status(factory, ids["node_id"], "DEGRADED")
    r = tc.post(f"/demo/nodes/{ids['node_id']}/recover", json={})
    assert r.status_code == 409
    assert "Traceback" not in r.text


def test_invalid_state_fail_non_failable_node_is_409(client):
    """A node status outside HEALTHY/DEGRADED/FAILED is a state conflict.

    failure_service documents that "any other node status raises
    InvalidNodeState and changes nothing", so the transition must be rejected
    without touching the node.
    """
    tc, factory = client
    ids = seed_exam(factory, sessions=1)
    _set_node_status(factory, ids["node_id"], "MAINTENANCE")
    r = tc.post(f"/demo/nodes/{ids['node_id']}/fail", json={})
    assert r.status_code == 409
    assert "Traceback" not in r.text

    db = factory()
    try:
        assert db.get(Node, ids["node_id"]).status == "MAINTENANCE"
    finally:
        db.close()


def test_infrastructure_unavailable_answer_is_503(client):
    """A FAILED node makes the session unable to accept work: 503, not 409."""
    tc, factory = client
    ids = seed_exam(factory, sessions=1)
    session = _start_one_session(tc, ids)
    tc.post(f"/demo/nodes/{ids['node_id']}/fail", json={})

    r = tc.post(
        f"/session/{session['session_id']}/answer",
        json={
            "question_id": ids["question_ids"][0],
            "answer": "A",
            "client_event_id": "down-1",
        },
    )
    assert r.status_code == 503


def test_infrastructure_unavailable_start_on_failed_node_is_503(client):
    """Starting a session on a FAILED node is an infrastructure outage."""
    tc, factory = client
    ids = seed_exam(factory, sessions=1)
    assert tc.post(f"/demo/nodes/{ids['node_id']}/fail", json={}).status_code == 200
    r = tc.post(
        "/session/start",
        json={
            "exam_id": ids["exam_id"],
            "candidate_id": ids["candidate_ids"][0],
            "node_id": ids["node_id"],
        },
    )
    assert r.status_code == 503


def test_error_bodies_carry_no_traceback_details(client):
    """Domain errors expose a clean message, never a stack trace."""
    tc, _ = client
    responses = [
        tc.get("/session/99999/state"),
        tc.get("/incident/99999"),
        tc.post("/demo/nodes/99999/fail", json={}),
        tc.get("/demo/overview/99999"),
    ]
    for r in responses:
        assert r.status_code == 404
        assert "Traceback" not in r.text
        assert 'File "' not in r.text
        assert "app/" not in r.text


def test_health_response_model_declares_exactly_status():
    """The declared contract is the single ``status`` string."""
    assert set(HealthResponse.model_fields) == {"status"}



# --------------------------------------------------------------------------- #
# Demo overview (primary frontend polling endpoint)
# --------------------------------------------------------------------------- #


def test_demo_overview_available_after_scenario_create(client):
    """The frontend can poll the overview right after provisioning."""
    tc, _ = client
    s = tc.post("/demo/scenario/create").json()
    r = tc.get(f"/demo/overview/{s['exam_id']}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "exam",
        "nodes",
        "sessions",
        "incidents",
        "audit_status",
        "latest_event_sequence_no",
    }
    assert body["nodes"][0]["status"] == "HEALTHY"
    assert body["audit_status"]["valid"] is True


def test_demo_overview_never_calls_the_ai_provider(client, monkeypatch):
    """The polling endpoint must never reach Gemini."""
    tc, _ = client
    s = tc.post("/demo/scenario/create").json()

    def explode(_prompt):
        raise AssertionError("overview must not call Gemini")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", explode)
    assert tc.get(f"/demo/overview/{s['exam_id']}").status_code == 200


def test_demo_overview_is_read_only(client):
    """Polling the overview writes nothing to the ledger."""
    tc, factory = client
    s = tc.post("/demo/scenario/create").json()
    _start_one_session(tc, s)
    for _ in range(3):
        assert tc.get(f"/demo/overview/{s['exam_id']}").status_code == 200
    from app.db import Event, Incident, Response, Session as ExamSession
    from sqlalchemy import func, select

    db = factory()
    try:
        assert db.scalar(select(func.count()).select_from(Event)) == 1
        assert db.scalar(select(func.count()).select_from(Incident)) == 0
        assert db.scalar(select(func.count()).select_from(ExamSession)) == 1
        assert db.scalar(select(func.count()).select_from(Response)) == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Controlled AI provider failures
# --------------------------------------------------------------------------- #


def _open_incident(tc, factory):
    """Seed, start a session, fail the node, evaluate -> incident ids."""
    ids = seed_exam(factory, sessions=1)
    _start_one_session(tc, ids)
    tc.post(f"/demo/nodes/{ids['node_id']}/fail", json={})
    incidents = tc.post(
        "/demo/incidents/evaluate", json={"exam_id": ids["exam_id"]}
    ).json()["incidents"]
    assert incidents
    return ids, incidents


def test_ai_missing_api_key_is_503(client, monkeypatch):
    """A missing credential is a controlled 503, not a crash."""
    tc, factory = client
    _, incidents = _open_incident(tc, factory)
    monkeypatch.delenv(ai_incident_service.GEMINI_API_KEY_ENV, raising=False)

    r = tc.post(f"/incident/{incidents[0]['id']}/analyze")
    assert r.status_code == 503
    assert "Traceback" not in r.text
    assert "GEMINI_API_KEY" in r.json()["detail"]


def test_ai_provider_error_is_502(client, monkeypatch):
    """An upstream Gemini failure is a controlled 502."""
    tc, factory = client
    _, incidents = _open_incident(tc, factory)

    def boom(_prompt):
        raise ai_incident_service.GeminiRequestError("upstream 503 from provider")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", boom)
    r = tc.post(f"/incident/{incidents[0]['id']}/analyze")
    assert r.status_code == 502
    assert "Traceback" not in r.text
    assert "upstream 503 from provider" in r.json()["detail"]


def test_ai_ungrounded_response_is_502(client, monkeypatch):
    """A hallucinated reference never reaches the client."""
    tc, factory = client
    _, incidents = _open_incident(tc, factory)

    def hallucinate(_prompt):
        return types.SimpleNamespace(
            parsed=None,
            candidates=[
                types.SimpleNamespace(
                    content=types.SimpleNamespace(
                        parts=[
                            types.SimpleNamespace(
                                text=json.dumps(
                                    {
                                        "likely_cause": "c",
                                        "impact_summary": "i",
                                        "recommended_response": "r",
                                        "evidence_refs": [987654],
                                    }
                                )
                            )
                        ]
                    )
                )
            ],
        )

    monkeypatch.setattr(ai_incident_service, "_call_gemini", hallucinate)
    r = tc.post(f"/incident/{incidents[0]['id']}/analyze")
    assert r.status_code == 502
    assert "ungrounded" in r.json()["detail"]


def test_ai_failure_does_not_break_core_endpoints(client, monkeypatch):
    """A provider outage leaves incidents, evidence and audit working."""
    tc, factory = client
    _, incidents = _open_incident(tc, factory)
    incident_id = incidents[0]["id"]

    def boom(_prompt):
        raise ai_incident_service.GeminiRequestError("provider down")

    monkeypatch.setattr(ai_incident_service, "_call_gemini", boom)
    assert tc.post(f"/incident/{incident_id}/analyze").status_code == 502
    assert tc.get(f"/incident/{incident_id}").status_code == 200
    assert tc.get(f"/incident/{incident_id}/evidence").status_code == 200
    assert tc.post("/audit/verify").json()["valid"] is True


# --------------------------------------------------------------------------- #
# OpenAPI document
# --------------------------------------------------------------------------- #

EXPECTED_OPERATIONS = {
    ("/health", "get"),
    ("/session/start", "post"),
    ("/session/{session_id}/answer", "post"),
    ("/session/{session_id}/heartbeat", "post"),
    ("/session/{session_id}/reconcile", "post"),
    ("/session/{session_id}/state", "get"),
    ("/incidents", "get"),
    ("/incident/{id}", "get"),
    ("/incident/{incident_id}/evidence", "get"),
    ("/incident/{incident_id}/analyze", "post"),
    ("/demo/incidents/evaluate", "post"),
    ("/demo/scenario/create", "post"),
    ("/demo/reset", "post"),
    ("/demo/overview/{exam_id}", "get"),
    ("/demo/nodes/{node_id}/health", "get"),
    ("/demo/nodes/{node_id}/fail", "post"),
    ("/demo/nodes/{node_id}/recover", "post"),
    ("/demo/tamper", "post"),
    ("/audit/verify", "post"),
}

EXPECTED_SCHEMAS = {
    "HealthResponse",
    "StartSessionRequest",
    "StartSessionResponse",
    "AnswerRequest",
    "AnswerResponse",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "ReconcileRequest",
    "ReconcileResponse",
    "FailNodeRequest",
    "FailNodeResponse",
    "RecoverNodeRequest",
    "RecoverNodeResponse",
    "IncidentResponse",
    "IncidentListResponse",
    "IncidentEvaluateRequest",
    "IncidentEvaluateResponse",
    "IncidentEvidenceResponse",
    "AIAnalysisResponse",
    "DemoTamperRequest",
    "DemoTamperResponse",
    "DemoScenarioCreateResponse",
    "DemoOverviewResponse",
    "NodeHealthResponse",
    "DemoResetResponse",
    "AuditVerifyResponse",
}


def _spec():
    return app.openapi()


def test_openapi_lists_every_current_endpoint():
    """The document advertises exactly the endpoints the API serves."""
    spec = _spec()
    actual = {
        (path, method)
        for path, operations in spec["paths"].items()
        for method in operations
    }
    assert actual == EXPECTED_OPERATIONS


def test_openapi_declares_a_response_model_for_every_endpoint():
    """Every operation returns a documented schema, not a bare object."""
    spec = _spec()
    components = spec["components"]["schemas"]
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            label = f"{method.upper()} {path}"
            ok = operation.get("responses", {}).get("200") or operation["responses"].get(
                "201"
            )
            assert ok is not None, f"{label} has no 2xx response"
            schema = ok.get("content", {}).get("application/json", {}).get("schema")
            assert schema is not None, f"{label} has no JSON schema"
            # Either an inline object or a $ref to a declared object schema.
            if "$ref" in schema:
                name = schema["$ref"].rsplit("/", 1)[-1]
                assert name in components, f"{label} refs missing schema {name}"
                assert components[name].get("type") == "object", f"{label}"
            else:
                assert schema.get("type") == "object", f"{label}"


def test_openapi_contains_the_expected_schemas():
    """All request/response models are present in components."""
    schemas = set(_spec()["components"]["schemas"])
    assert EXPECTED_SCHEMAS <= schemas


def test_openapi_health_schema_is_minimal():
    """The health contract stays a single status string."""
    schema = _spec()["components"]["schemas"]["HealthResponse"]
    assert schema["required"] == ["status"]
    assert set(schema["properties"]) == {"status"}


def test_openapi_analysis_response_has_no_status_field():
    """AIAnalysisResponse must not re-introduce a redundant status field."""
    schema = _spec()["components"]["schemas"]["AIAnalysisResponse"]
    assert set(schema["properties"]) == {
        "incident_id",
        "likely_cause",
        "impact_summary",
        "recommended_response",
        "evidence_refs",
        "generated_at",
    }


def test_openapi_overview_declares_the_frontend_poll_contract():
    """The polling endpoint documents its full operational payload."""
    schema = _spec()["components"]["schemas"]["DemoOverviewResponse"]
    assert set(schema["properties"]) == {
        "exam",
        "nodes",
        "sessions",
        "incidents",
        "audit_status",
        "latest_event_sequence_no",
    }
    assert set(schema["required"]) == set(schema["properties"])


def test_openapi_audit_verify_takes_no_request_body():
    """POST /audit/verify is a bodyless endpoint."""
    op = _spec()["paths"]["/audit/verify"]["post"]
    assert "requestBody" not in op


def test_openapi_exposes_no_authentication_scheme():
    """No auth was introduced."""
    spec = _spec()
    assert "securitySchemes" not in spec.get("components", {})
    for operations in spec["paths"].values():
        for operation in operations.values():
            assert "security" not in operation


def test_openapi_serves_over_http(client):
    """The live /openapi.json matches the in-process document."""
    tc, _ = client
    r = tc.get("/openapi.json")
    assert r.status_code == 200
    served = r.json()
    assert set(served["paths"]) == set(_spec()["paths"])

