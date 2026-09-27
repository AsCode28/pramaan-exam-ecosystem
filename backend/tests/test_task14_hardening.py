"""Task 14 tests: early-warning health, Gemini timeout, DEMO_MODE gating, reset.

Covers:
1. early-warning heartbeat signal (fresh / stale / recovered / failed /
   no-false-positive / read-only)
2. Gemini timeout configuration + provider failure handling
3. DEMO_MODE gating of destructive demo operations
4. safe demo reset producing a clean zero-event ledger
"""

import json
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

from app.core import demo_config
from app.core.database import get_db
from app.db import (
    Base,
    Candidate,
    Event,
    Exam,
    Node,
    Question,
)
from app.main import app
from app.services import ai_incident_service, audit_service


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_task14.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield f
    engine.dispose()


@pytest.fixture()
def client(factory, monkeypatch):
    monkeypatch.setenv(demo_config.DEMO_MODE_ENV, "true")
    app.dependency_overrides[get_db] = lambda: factory()
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.clear()


def seed_node(factory, status="HEALTHY"):
    """One exam + one node + one candidate + one question."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title="T14", status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()
        node = Node(exam_id=exam.id, status=status)
        db.add(node)
        db.flush()
        cand = Candidate(name="C", roll_no="T14-1")
        db.add(cand)
        db.flush()
        q = Question(exam_id=exam.id, text="Q?", marks=1)
        db.add(q)
        db.commit()
        return {"exam_id": exam.id, "node_id": node.id, "question_id": q.id}
    finally:
        db.close()


def add_heartbeat(factory, node_id, age_seconds, exam_id=None, client_timestamp=None):
    """Append a HEARTBEAT whose authoritative server_timestamp is `age` old."""
    db = factory()
    try:
        stamp = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=age_seconds
        )
        ev = Event(
            exam_id=exam_id,
            node_id=node_id,
            event_type="HEARTBEAT",
            payload={},
            server_timestamp=stamp,
            client_timestamp=client_timestamp,
        )
        db.add(ev)
        db.commit()
        return ev.sequence_no
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. Early-warning heartbeat signal
# --------------------------------------------------------------------------- #


def test_fresh_heartbeat_is_healthy(client, factory):
    """A heartbeat well inside the threshold reports HEALTHY."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=1, exam_id=ids["exam_id"])

    r = client.get(f"/demo/nodes/{ids['node_id']}/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["health_state"] == "HEALTHY"
    assert body["early_warning"] is False
    assert body["last_heartbeat_server_timestamp"] is not None
    assert 0 <= body["heartbeat_age_seconds"] < 5
    assert body["threshold_seconds"] == demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS


def test_no_false_degradation_inside_threshold(client, factory):
    """A heartbeat just under the threshold must NOT be flagged."""
    threshold = demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    ids = seed_node(factory)
    add_heartbeat(
        factory, ids["node_id"], age_seconds=threshold - 5, exam_id=ids["exam_id"]
    )

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["health_state"] == "HEALTHY", body
    assert body["early_warning"] is False
    assert body["heartbeat_age_seconds"] < threshold


def test_stale_heartbeat_is_degraded_early_warning(client, factory):
    """A heartbeat past the threshold reports DEGRADED as an early warning."""
    threshold = demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    ids = seed_node(factory)
    add_heartbeat(
        factory, ids["node_id"], age_seconds=threshold + 30, exam_id=ids["exam_id"]
    )

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["health_state"] == "DEGRADED"
    assert body["early_warning"] is True
    assert body["heartbeat_age_seconds"] > threshold
    assert "early warning" in body["reason"].lower()


def test_healthy_recovery_after_a_new_heartbeat(client, factory):
    """A fresh heartbeat clears the early-warning signal."""
    threshold = demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    ids = seed_node(factory)
    add_heartbeat(
        factory, ids["node_id"], age_seconds=threshold + 60, exam_id=ids["exam_id"]
    )
    assert (
        client.get(f"/demo/nodes/{ids['node_id']}/health").json()["health_state"]
        == "DEGRADED"
    )

    add_heartbeat(factory, ids["node_id"], age_seconds=0, exam_id=ids["exam_id"])

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["health_state"] == "HEALTHY"


def test_failed_node_stays_failed(client, factory):
    """A FAILED node is FAILED, never merely 'stale'."""
    ids = seed_node(factory, status="HEALTHY")
    add_heartbeat(
        factory,
        ids["node_id"],
        age_seconds=demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS + 60,
        exam_id=ids["exam_id"],
    )
    client.post(f"/demo/nodes/{ids['node_id']}/fail", json={})

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["health_state"] == "FAILED"
    assert body["early_warning"] is False


def test_health_uses_server_timestamp_not_client_timestamp(client, factory):
    """Freshness comes from server_timestamp; client_timestamp is ignored."""
    threshold = demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    ids = seed_node(factory)
    # Server timestamp fresh, client timestamp looks ancient.
    add_heartbeat(
        factory,
        ids["node_id"],
        age_seconds=0,
        exam_id=ids["exam_id"],
        client_timestamp=datetime(2000, 1, 1),
    )

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["heartbeat_age_seconds"] < threshold
    assert body["health_state"] == "HEALTHY"


def test_threshold_is_configurable(client, factory, monkeypatch):
    """HEARTBEAT_STALE_SECONDS changes the boundary."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=100, exam_id=ids["exam_id"])

    monkeypatch.setenv(demo_config.HEARTBEAT_STALE_SECONDS_ENV, "200")
    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["threshold_seconds"] == 200
    assert body["health_state"] == "HEALTHY"

    monkeypatch.setenv(demo_config.HEARTBEAT_STALE_SECONDS_ENV, "30")
    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["threshold_seconds"] == 30
    assert body["health_state"] == "DEGRADED"


def test_no_heartbeat_evidence_reports_healthy_without_fabrication(client, factory):
    """No heartbeat means no evidence, so no degradation is invented."""
    ids = seed_node(factory)
    before = client.post("/audit/verify").json()["events_checked"]

    body = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    assert body["health_state"] == "HEALTHY"
    assert body["early_warning"] is False
    assert body["last_heartbeat_server_timestamp"] is None
    assert "no heartbeat recorded yet" in body["reason"]
    # No synthetic event was created to make the signal look predictive.
    assert client.post("/audit/verify").json()["events_checked"] == before


def test_health_endpoint_never_mutates_state(client, factory):
    """Polling the health signal writes nothing to the ledger or nodes."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=5, exam_id=ids["exam_id"])

    def snapshot():
        db = factory()
        try:
            return (
                db.scalar(select(func.count()).select_from(Event)),
                db.scalar(select(func.count()).select_from(Node)),
                db.get(Node, ids["node_id"]).status,
            )
        finally:
            db.close()

    before = snapshot()
    for _ in range(3):
        client.get(f"/demo/nodes/{ids['node_id']}/health")
    assert snapshot() == before


def test_health_unknown_node_404(client):
    assert client.get("/demo/nodes/99999/health").status_code == 404




# --------------------------------------------------------------------------- #
# 5. Node health embedded in the demo overview
# --------------------------------------------------------------------------- #


def _overview_nodes(client, exam_id):
    r = client.get(f"/demo/overview/{exam_id}")
    assert r.status_code == 200, r.text
    return r.json()["nodes"]


def test_overview_nodes_include_health(client, factory):
    """Every overview node carries the nested health signal."""
    ids = seed_node(factory)
    nodes = _overview_nodes(client, ids["exam_id"])

    assert len(nodes) == 1
    health = nodes[0]["health"]
    assert health["node_id"] == ids["node_id"]
    assert health["health_state"] == "HEALTHY"
    assert health["early_warning"] is False
    assert health["threshold_seconds"] == demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    assert "reason" in health
    # Existing node fields are preserved.
    assert nodes[0]["id"] == ids["node_id"]
    assert nodes[0]["exam_id"] == ids["exam_id"]
    assert nodes[0]["status"] == "HEALTHY"


def test_overview_health_fresh_heartbeat_is_healthy(client, factory):
    """A fresh heartbeat surfaces HEALTHY in the overview."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=1, exam_id=ids["exam_id"])

    health = _overview_nodes(client, ids["exam_id"])[0]["health"]
    assert health["health_state"] == "HEALTHY"
    assert health["early_warning"] is False
    assert health["last_heartbeat_server_timestamp"] is not None
    assert health["heartbeat_age_seconds"] < demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS


def test_overview_health_stale_heartbeat_is_degraded(client, factory):
    """A stale heartbeat surfaces DEGRADED with early_warning=true."""
    threshold = demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS
    ids = seed_node(factory)
    add_heartbeat(
        factory, ids["node_id"], age_seconds=threshold + 30, exam_id=ids["exam_id"]
    )

    health = _overview_nodes(client, ids["exam_id"])[0]["health"]
    assert health["health_state"] == "DEGRADED"
    assert health["early_warning"] is True
    assert health["heartbeat_age_seconds"] > threshold


def test_overview_health_failed_node_reports_failed(client, factory):
    """A FAILED node reports FAILED in the overview health signal."""
    ids = seed_node(factory)
    add_heartbeat(
        factory,
        ids["node_id"],
        age_seconds=demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS + 60,
        exam_id=ids["exam_id"],
    )
    client.post(f"/demo/nodes/{ids['node_id']}/fail", json={})

    node = _overview_nodes(client, ids["exam_id"])[0]
    assert node["status"] == "FAILED"
    assert node["health"]["health_state"] == "FAILED"
    assert node["health"]["early_warning"] is False


def test_overview_health_matches_the_health_endpoint(client, factory):
    """The overview reuses the same service, so both agree exactly."""
    ids = seed_node(factory)
    add_heartbeat(
        factory,
        ids["node_id"],
        age_seconds=demo_config.DEFAULT_HEARTBEAT_STALE_SECONDS + 5,
        exam_id=ids["exam_id"],
    )
    from_endpoint = client.get(f"/demo/nodes/{ids['node_id']}/health").json()
    from_overview = _overview_nodes(client, ids["exam_id"])[0]["health"]

    for key in (
        "node_id",
        "health_state",
        "last_heartbeat_server_timestamp",
        "threshold_seconds",
        "reason",
        "early_warning",
    ):
        assert from_overview[key] == from_endpoint[key], key
    # The age is recomputed per call, so allow a small drift rather than equality.
    assert abs(
        from_overview["heartbeat_age_seconds"] - from_endpoint["heartbeat_age_seconds"]
    ) < 5


def test_overview_remains_read_only_with_health(client, factory):
    """Polling the overview still appends nothing to the ledger."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=2, exam_id=ids["exam_id"])
    before = client.post("/audit/verify").json()["events_checked"]

    for _ in range(3):
        client.get(f"/demo/overview/{ids['exam_id']}")

    assert client.post("/audit/verify").json()["events_checked"] == before


# --------------------------------------------------------------------------- #
# 2. Gemini timeout and provider failure hardening
# --------------------------------------------------------------------------- #


def test_gemini_timeout_is_finite_and_configurable(monkeypatch):
    """Timeout defaults to a finite value and honours the env var."""
    monkeypatch.delenv(demo_config.GEMINI_TIMEOUT_MS_ENV, raising=False)
    default = ai_incident_service.get_timeout_ms()
    assert default == demo_config.DEFAULT_GEMINI_TIMEOUT_MS
    assert 0 < default < 10**7  # finite, not infinite

    monkeypatch.setenv(demo_config.GEMINI_TIMEOUT_MS_ENV, "5000")
    assert ai_incident_service.get_timeout_ms() == 5000

    # An unparsable or non-positive value falls back to the safe default.
    monkeypatch.setenv(demo_config.GEMINI_TIMEOUT_MS_ENV, "not-a-number")
    assert ai_incident_service.get_timeout_ms() == default
    monkeypatch.setenv(demo_config.GEMINI_TIMEOUT_MS_ENV, "-1")
    assert ai_incident_service.get_timeout_ms() == default


def test_gemini_request_carries_the_timeout(monkeypatch):
    """The timeout is passed to the SDK's supported http_options mechanism."""
    from google.genai import types

    class FakeResponse:
        parsed = None
        candidates = []

    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "k")
    monkeypatch.setenv(demo_config.GEMINI_TIMEOUT_MS_ENV, "12345")
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    import google.genai as real_genai

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(real_genai, "Client", FakeClient)
    ai_incident_service._call_gemini("prompt")

    http_options = captured["config"]["http_options"]
    assert isinstance(http_options, types.HttpOptions)
    assert http_options.timeout == 12345
    assert captured["config"]["response_schema"] is ai_incident_service.IncidentAnalysis


def test_gemini_timeout_becomes_a_controlled_error(monkeypatch):
    """A provider timeout is wrapped, never leaks a traceback."""
    monkeypatch.setenv(ai_incident_service.GEMINI_API_KEY_ENV, "k")

    import google.genai as real_genai

    class TimingOutModels:
        def generate_content(self, **kwargs):
            raise TimeoutError("read timed out")

    class TimingOutClient:
        def __init__(self, api_key):
            self.models = TimingOutModels()

    monkeypatch.setattr(real_genai, "Client", TimingOutClient)
    with pytest.raises(ai_incident_service.GeminiRequestError) as excinfo:
        ai_incident_service._call_gemini("prompt")
    assert "read timed out" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 3. DEMO_MODE gating
# --------------------------------------------------------------------------- #


def test_demo_mode_defaults_to_off(monkeypatch):
    """The safe default for a non-demo environment is disabled."""
    monkeypatch.delenv(demo_config.DEMO_MODE_ENV, raising=False)
    assert demo_config.is_demo_mode() is False
    for raw in ("", "0", "false", "no", "off", "banana"):
        monkeypatch.setenv(demo_config.DEMO_MODE_ENV, raw)
        assert demo_config.is_demo_mode() is False
    for raw in ("1", "true", "TRUE", "yes", "on", "enabled"):
        monkeypatch.setenv(demo_config.DEMO_MODE_ENV, raw)
        assert demo_config.is_demo_mode() is True


def test_destructive_endpoints_allowed_when_demo_mode_on(client, factory):
    """With DEMO_MODE on, failure/recovery/tamper work normally."""
    ids = seed_node(factory)
    r = client.post(f"/demo/nodes/{ids['node_id']}/fail", json={})
    assert r.status_code == 200, r.text
    assert r.json()["new_status"] == "FAILED"
    r = client.post(f"/demo/nodes/{ids['node_id']}/recover", json={})
    assert r.status_code == 200, r.text
    r = client.post("/demo/tamper", json={"target": "latest", "field": "payload"})
    assert r.status_code == 200, r.text


def test_destructive_endpoints_rejected_when_demo_mode_off(client, factory, monkeypatch):
    """With DEMO_MODE off, every destructive operation is refused with 403."""
    ids = seed_node(factory)
    monkeypatch.delenv(demo_config.DEMO_MODE_ENV, raising=False)

    fail = client.post(f"/demo/nodes/{ids['node_id']}/fail", json={})
    assert fail.status_code == 403
    assert "DEMO_MODE" in fail.json()["detail"]

    recover = client.post(f"/demo/nodes/{ids['node_id']}/recover", json={})
    assert recover.status_code == 403

    tamper = client.post("/demo/tamper", json={"target": "latest", "field": "payload"})
    assert tamper.status_code == 403

    reset = client.post("/demo/reset")
    assert reset.status_code == 403


def test_readonly_endpoints_unaffected_by_demo_mode_off(client, factory, monkeypatch):
    """Monitoring stays available with DEMO_MODE off."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=1, exam_id=ids["exam_id"])
    monkeypatch.delenv(demo_config.DEMO_MODE_ENV, raising=False)

    assert client.get("/health").status_code == 200
    assert client.post("/audit/verify").status_code == 200
    assert client.get("/incidents", params={"exam_id": ids["exam_id"]}).status_code == 200
    assert client.get(f"/demo/overview/{ids['exam_id']}").status_code == 200
    assert client.get(f"/demo/nodes/{ids['node_id']}/health").status_code == 200
    assert client.post("/demo/scenario/create").status_code == 200


def test_rejected_failure_does_not_mutate_anything(client, factory, monkeypatch):
    """A gated failure injection changes no state at all."""
    ids = seed_node(factory)
    monkeypatch.delenv(demo_config.DEMO_MODE_ENV, raising=False)
    before = client.post("/audit/verify").json()["events_checked"]

    assert client.post(f"/demo/nodes/{ids['node_id']}/fail", json={}).status_code == 403

    assert client.post("/audit/verify").json()["events_checked"] == before
    db = factory()
    try:
        assert db.get(Node, ids["node_id"]).status == "HEALTHY"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4. Safe demo reset
# --------------------------------------------------------------------------- #


def test_reset_requires_demo_mode(client, factory, monkeypatch):
    """Reset is refused when demo mode is off, and nothing is wiped."""
    ids = seed_node(factory)
    add_heartbeat(factory, ids["node_id"], age_seconds=0, exam_id=ids["exam_id"])
    before = client.post("/audit/verify").json()["events_checked"]
    assert before > 0

    monkeypatch.delenv(demo_config.DEMO_MODE_ENV, raising=False)
    r = client.post("/demo/reset")
    assert r.status_code == 403
    assert client.post("/audit/verify").json()["events_checked"] == before


def test_reset_refuses_non_sqlite_database(monkeypatch):
    """A production-style URL is protected from the HTTP reset."""
    from app.services import demo_reset_service

    monkeypatch.setenv(demo_config.DEMO_MODE_ENV, "true")
    monkeypatch.setattr(
        demo_reset_service, "DATABASE_URL", "postgresql://user:pw@host/db"
    )
    with pytest.raises(demo_reset_service.DemoResetError) as excinfo:
        demo_reset_service._assert_local_sqlite()
    assert "only supported for the local SQLite prototype" in str(excinfo.value)
