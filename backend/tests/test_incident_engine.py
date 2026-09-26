"""Isolated API tests for the Incident Detection & Escalation engine (Task 6).

Uses a temporary SQLite database per test via FastAPI dependency_overrides;
the production pramaan.db is never touched.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.core.hashing import verify_event
from app.db import (
    Base,
    Candidate,
    Event,
    Exam,
    Incident,
    IncidentSession,
    Node,
    Question,
    Session,
)
from app.main import app
from app.services import incident_service


@pytest.fixture()
def client(tmp_path):
    """TestClient wired to an isolated SQLite database."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_incidents.db",
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


def seed(factory, node_count=2, sessions_per_node=2, questions=2, title="E1"):
    """One exam + nodes + candidates + ACTIVE sessions + questions."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        exam = Exam(title=title, status="ACTIVE", start_time=now, end_time=now)
        db.add(exam)
        db.flush()

        nodes = [Node(exam_id=exam.id, status="HEALTHY") for _ in range(node_count)]
        qs = [
            Question(exam_id=exam.id, text=f"Q{i}?", marks=1) for i in range(questions)
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
            # session ids grouped per node, in node order
            "session_ids": [s.id for s in sessions],
            "sessions_by_node": [
                [s.id for s in sessions if s.node_id == n.id] for n in nodes
            ],
        }
    finally:
        db.close()


def add_sessions(factory, exam_id, node_id, count, prefix="S"):
    """Append `count` extra ACTIVE sessions on node_id; returns their ids."""
    db = factory()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        created = []
        for index in range(count):
            candidate = Candidate(
                name=f"{prefix}C{index}", roll_no=f"{prefix}-{exam_id}-{index}"
            )
            db.add(candidate)
            db.flush()
            session = Session(
                exam_id=exam_id,
                candidate_id=candidate.id,
                node_id=node_id,
                status="ACTIVE",
                started_at=now,
            )
            db.add(session)
            db.flush()
            created.append(session.id)
        db.commit()
        return created
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


def reconcile(tc, session_id, question_id, key):
    """Reconcile one buffered answer; this is the only path back to ACTIVE."""
    r = tc.post(
        f"/session/{session_id}/reconcile",
        json={
            "events": [
                {
                    "client_event_id": key,
                    "question_id": question_id,
                    "answer": "A",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ACTIVE", body
    return body


def evaluate(tc, exam_id):
    r = tc.post("/demo/incidents/evaluate", json={"exam_id": exam_id})
    assert r.status_code == 200, r.text
    return r.json()["incidents"]


def failure_events(factory, exam_id):
    """NODE_FAILURE_INJECTED events of one exam, chronologically."""
    db = factory()
    try:
        return db.scalars(
            select(Event)
            .where(
                Event.exam_id == exam_id,
                Event.event_type == "NODE_FAILURE_INJECTED",
            )
            .order_by(Event.sequence_no.asc())
        ).all()
    finally:
        db.close()


def by_severity(incidents, severity):
    return [i for i in incidents if i["severity"] == severity]


def test_healthy_exam_produces_no_incidents(client):
    """A fully healthy exam projects zero incidents."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=2)
    assert evaluate(tc, ids["exam_id"]) == []


def test_isolated_session_failure_creates_candidate_incident_only(client):
    """One affected session below every aggregate threshold -> exactly one CANDIDATE."""
    tc, factory = client
    # 20 sessions total, only 1 affected -> 5% (<=15%) and 1 (<3): no NODE/SYSTEM.
    ids = seed(factory, node_count=2, sessions_per_node=1, questions=0)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 19, "X")

    fail_node(tc, ids["node_ids"][0])
    victim = ids["session_ids"][0]

    incidents = evaluate(tc, ids["exam_id"])
    assert len(incidents) == 1
    cand = incidents[0]
    assert cand["severity"] == "CANDIDATE"
    assert cand["status"] == "ACTIVE"
    assert cand["affected_session_ids"] == [victim]
    assert cand["resolved_at"] is None


def test_candidate_incident_is_per_session_not_shared(client):
    """Each affected session gets its own independent CANDIDATE incident."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=3, questions=0)
    victims = ids["sessions_by_node"][0]

    fail_node(tc, ids["node_ids"][0])

    incidents = evaluate(tc, ids["exam_id"])
    cands = by_severity(incidents, "CANDIDATE")
    assert len(cands) == 3
    assert sorted(c["affected_session_ids"][0] for c in cands) == sorted(victims)
    # Three separate rows, never merged into one.
    assert len({c["id"] for c in cands}) == 3


def test_node_incident_uses_origin_event_node_id(client):
    ">=3 sessions on one node inside 60s creates a NODE incident keyed by origin event."
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3)
    failed = fail_node(tc, ids["node_ids"][0])

    incidents = evaluate(tc, ids["exam_id"])
    nodes = by_severity(incidents, "NODE")
    assert len(nodes) == 1
    node_inc = nodes[0]
    assert node_inc["created_from_event_id"] == failed["event_sequence_no"]
    assert sorted(node_inc["affected_session_ids"]) == sorted(ids["session_ids"])

    # The CANDIDATE incidents remain separate rows, untouched by aggregation.
    assert len(by_severity(incidents, "CANDIDATE")) == 3


def test_node_incident_identity_comes_from_join_not_new_column(client):
    """NODE scope resolves through created_from_event_id -> Event.node_id."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=3)

    fail_node(tc, ids["node_ids"][0])
    fail_node(tc, ids["node_ids"][1])

    incidents = evaluate(tc, ids["exam_id"])
    nodes = by_severity(incidents, "NODE")
    assert len(nodes) == 2

    db = factory()
    try:
        origin_nodes = set()
        for inc in nodes:
            origin = db.get(Event, inc["created_from_event_id"])
            assert origin is not None
            assert origin.event_type == "NODE_FAILURE_INJECTED"
            origin_nodes.add(origin.node_id)
        assert origin_nodes == set(ids["node_ids"])

        # find_active_node_incident resolves the same way.
        for nid in ids["node_ids"]:
            assert incident_service.find_active_node_incident(
                db, ids["exam_id"], nid
            ) is not None
    finally:
        db.close()


def test_node_threshold_requires_three_affected_sessions(client):
    """Two affected sessions stay below the NODE threshold."""
    tc, factory = client
    # 40 sessions; node 1 holds 2 -> below the 3-session threshold, 5% <= 15%.
    ids = seed(factory, node_count=2, sessions_per_node=2, questions=0)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 38, "M")

    fail_node(tc, ids["node_ids"][0])

    incidents = evaluate(tc, ids["exam_id"])
    assert len(by_severity(incidents, "CANDIDATE")) == 2
    assert by_severity(incidents, "NODE") == []
    assert by_severity(incidents, "SYSTEM") == []


def test_node_threshold_event_derived_outside_window_creates_nothing(client):
    """Failure older than the 60s window cannot open a NEW NODE incident."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=40, questions=0)
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=300)

    db = factory()
    try:
        node = db.get(Node, ids["node_ids"][0])
        node.status = "FAILED"
        victims = [db.get(Session, sid) for sid in ids["session_ids"][:3]]
        for sess in victims:
            sess.status = "DISCONNECTED"
        db.add(
            Event(
                exam_id=ids["exam_id"],
                node_id=node.id,
                event_type="NODE_FAILURE_INJECTED",
                payload={
                    "previous_status": "HEALTHY",
                    "new_status": "FAILED",
                    "affected_session_ids": [s.id for s in victims],
                },
                server_timestamp=stale,
            )
        )
        db.commit()
    finally:
        db.close()

    incidents = evaluate(tc, ids["exam_id"])
    assert len(by_severity(incidents, "CANDIDATE")) == 3
    assert by_severity(incidents, "NODE") == []


def test_healthy_node_with_affected_sessions_opens_no_node_incident(client):
    """A HEALTHY node never opens a NEW NODE incident from historical evidence.

    The node recovered (HEALTHY) while its sessions are still RECOVERING. The
    3-in-60s failure evidence is still inside the window, but the operational
    condition is gone, so only an already-open incident could be reused.
    """
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    node_id = ids["node_ids"][0]
    qid = ids["question_ids"][0]
    sessions = ids["session_ids"]

    fail_node(tc, node_id)
    db = factory()
    try:
        node = db.get(Node, node_id)
        node.status = "HEALTHY"  # node recovered
        for sid in sessions:
            db.get(Session, sid).status = "RECOVERING"  # sessions still affected
        db.commit()
    finally:
        db.close()

    incidents = evaluate(tc, ids["exam_id"])
    assert by_severity(incidents, "NODE") == []
    # The sessions are still legitimately tracked per candidate.
    assert len(by_severity(incidents, "CANDIDATE")) == 3
    for candidate in by_severity(incidents, "CANDIDATE"):
        assert candidate["created_from_event_id"] is not None

    # And no NODE row exists in the table either.
    db = factory()
    try:
        assert (
            db.scalars(
                select(Incident).where(Incident.severity == "NODE")
            ).all()
            == []
        )
    finally:
        db.close()


def test_affected_session_without_causal_event_opens_no_candidate_incident(client):
    """An affected session with no causal failure event is never persisted."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=2, questions=0)
    anomalous = ids["session_ids"][0]

    db = factory()
    try:
        # Anomalous state: affected session, but the ledger holds no
        # NODE_FAILURE_INJECTED for this exam at all.
        db.get(Session, anomalous).status = "DISCONNECTED"
        db.commit()
    finally:
        db.close()

    incidents = evaluate(tc, ids["exam_id"])
    assert by_severity(incidents, "CANDIDATE") == []

    db = factory()
    try:
        candidates = db.scalars(
            select(Incident).where(Incident.severity == "CANDIDATE")
        ).all()
        assert candidates == []
        # The anomalous session is attached to no CANDIDATE incident.
        assert (
            db.scalars(
                select(IncidentSession)
                .join(Incident, IncidentSession.incident_id == Incident.id)
                .where(
                    IncidentSession.session_id == anomalous,
                    Incident.severity == "CANDIDATE",
                )
            ).all()
            == []
        )
    finally:
        db.close()


def test_active_node_incident_survives_the_60s_window(client):
    """An open NODE incident is reused, not re-created, once the window rolls past."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3)
    fail_node(tc, ids["node_ids"][0])

    first = by_severity(evaluate(tc, ids["exam_id"]), "NODE")
    assert len(first) == 1
    node_inc_id = first[0]["id"]

    db = factory()
    try:
        for ev in db.scalars(select(Event)).all():
            ev.server_timestamp = ev.server_timestamp - timedelta(seconds=600)
        db.commit()
    finally:
        db.close()

    second = by_severity(evaluate(tc, ids["exam_id"]), "NODE")
    assert len(second) == 1
    assert second[0]["id"] == node_inc_id
    assert second[0]["status"] == "ACTIVE"


def test_repeated_evaluation_is_idempotent(client):
    """Re-evaluating the same state creates no duplicate rows or links."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3)
    fail_node(tc, ids["node_ids"][0])

    first = evaluate(tc, ids["exam_id"])
    second = evaluate(tc, ids["exam_id"])
    third = evaluate(tc, ids["exam_id"])

    first_ids = [i["id"] for i in first]
    assert first_ids == [i["id"] for i in second] == [i["id"] for i in third]

    db = factory()
    try:
        rows = db.scalars(
            select(Incident).where(Incident.exam_id == ids["exam_id"])
        ).all()
        assert len(rows) == len(first)
        links = db.scalars(
            select(IncidentSession).where(
                IncidentSession.incident_id.in_([r.id for r in rows])
            )
        ).all()
        pairs = [(link.incident_id, link.session_id) for link in links]
        assert len(pairs) == len(set(pairs))
    finally:
        db.close()


def test_system_incident_from_two_failed_nodes(client):
    """>= 2 failed nodes opens one SYSTEM incident scoped to the exam."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=20)

    first_fail = fail_node(tc, ids["node_ids"][0])
    fail_node(tc, ids["node_ids"][1])

    incidents = evaluate(tc, ids["exam_id"])
    systems = by_severity(incidents, "SYSTEM")
    assert len(systems) == 1
    # Chronologically first threshold crossing is the origin event.
    assert systems[0]["created_from_event_id"] == first_fail["event_sequence_no"]
    assert systems[0]["affected_session_ids"]


def test_system_incident_from_affected_session_ratio(client):
    """The affected-ratio threshold alone opens a SYSTEM incident."""
    tc, factory = client
    # 5 of 20 sessions = 25% > 15%, with only ONE failed node, so the
    # >= 2 failed nodes trigger cannot be what opened this incident.
    ids = seed(factory, node_count=2, sessions_per_node=5, questions=0)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 10, "M")

    fail_node(tc, ids["node_ids"][0])

    incidents = evaluate(tc, ids["exam_id"])
    assert len(by_severity(incidents, "CANDIDATE")) == 5
    systems = by_severity(incidents, "SYSTEM")
    assert len(systems) == 1
    assert systems[0]["status"] == "ACTIVE"


def test_system_threshold_is_strictly_greater_than_15_percent(client):
    """Exactly 15% affected is NOT a system incident."""
    tc, factory = client
    # 20 sessions, node 1 holds 3 -> 3/20 = 15% exactly, which is not > 15%.
    ids = seed(factory, node_count=2, sessions_per_node=3, questions=0)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 14, "M")

    fail_node(tc, ids["node_ids"][0])

    incidents = evaluate(tc, ids["exam_id"])
    assert len(by_severity(incidents, "CANDIDATE")) == 3
    assert by_severity(incidents, "SYSTEM") == []


def test_two_historical_failure_events_are_not_two_current_failed_nodes(client):
    """A recovered node never counts towards the >= 2 failed-node threshold."""
    tc, factory = client
    # 14 sessions; nodes 1 and 2 hold 1 session each, so 1/14 = 7.1% <= 15%.
    ids = seed(factory, node_count=4, sessions_per_node=1, questions=1)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][2], 5, "M")
    add_sessions(factory, ids["exam_id"], ids["node_ids"][3], 6, "P")
    n1, n2 = ids["node_ids"][0], ids["node_ids"][1]
    qid = ids["question_ids"][0]

    # Node 1 fails and fully recovers.
    fail_node(tc, n1)
    recover_node(tc, n1)
    reconcile(tc, ids["sessions_by_node"][0][0], qid, "a-1")

    # Node 2 fails and stays down: only ONE node is currently FAILED.
    fail_node(tc, n2)

    events = failure_events(factory, ids["exam_id"])
    assert len(events) == 2  # two historical failure events exist

    crossing = incident_service._system_threshold_crossing(
        events,
        current_failed_node_ids={n2},
        current_affected_session_ids=set(ids["sessions_by_node"][1]),
        total_sessions=14,
    )
    assert crossing is None

    assert by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM") == []


def test_recovered_sessions_do_not_satisfy_the_ratio_threshold(client):
    """Only sessions that are still affected may satisfy the > 15% threshold."""
    tc, factory = client
    # 20 sessions; node 1 holds 5 -> 5/20 = 25% while all are affected.
    ids = seed(factory, node_count=2, sessions_per_node=5, questions=1)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 10, "M")
    n1 = ids["node_ids"][0]
    victims = ids["sessions_by_node"][0]
    qid = ids["question_ids"][0]

    injected = fail_node(tc, n1)
    events = failure_events(factory, ids["exam_id"])
    assert len(events) == 1

    # All 5 still affected -> 25% > 15% -> the failure event is the crossing.
    assert (
        incident_service._system_threshold_crossing(
            events,
            current_failed_node_ids={n1},
            current_affected_session_ids=set(victims),
            total_sessions=20,
        )
        == injected["event_sequence_no"]
    )

    # Every victim recovers: the same old event must no longer cross.
    recover_node(tc, n1)
    for index, session_id in enumerate(victims):
        reconcile(tc, session_id, qid, f"r-{index}")

    assert (
        incident_service._system_threshold_crossing(
            events,
            current_failed_node_ids=set(),
            current_affected_session_ids=set(),
            total_sessions=20,
        )
        is None
    )
    # And the engine opens nothing: node is HEALTHY, no session is affected.
    assert by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM") == []


def test_crossing_event_is_the_first_event_crossing_the_current_condition(client):
    """created_from_event_id is the first event that crosses the CURRENT state."""
    tc, factory = client
    # 20 sessions; nodes 1 and 2 hold 2 each, so a single failure is 10% <= 15%.
    ids = seed(factory, node_count=2, sessions_per_node=2, questions=0)
    add_sessions(factory, ids["exam_id"], ids["node_ids"][1], 16, "M")

    first = fail_node(tc, ids["node_ids"][0])
    # One failed node and 2/20 affected: neither threshold is met yet.
    assert by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM") == []

    second = fail_node(tc, ids["node_ids"][1])
    # Now 2 currently-failed nodes AND 4/20 = 20% > 15%; the first event that
    # pushes the CURRENT condition over the line is the second failure.
    systems = by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM")
    assert len(systems) == 1
    assert systems[0]["created_from_event_id"] == second["event_sequence_no"]
    assert systems[0]["created_from_event_id"] != first["event_sequence_no"]


def test_active_condition_without_any_crossing_event_is_not_persisted(client):
    """An unexplainable active condition leaves the projection unchanged."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=10, questions=0)
    affected = ids["session_ids"][:2]

    # The system condition IS active (2 currently FAILED nodes and affected
    # sessions), but the ledger holds no NODE_FAILURE_INJECTED at all, so no
    # real event can explain it.
    db = factory()
    try:
        for node_id in ids["node_ids"]:
            db.get(Node, node_id).status = "FAILED"
        for session_id in affected:
            db.get(Session, session_id).status = "DISCONNECTED"
        db.commit()
    finally:
        db.close()

    assert failure_events(factory, ids["exam_id"]) == []

    incidents = evaluate(tc, ids["exam_id"])
    assert by_severity(incidents, "SYSTEM") == []

    db = factory()
    try:
        # Nothing was persisted, so no ungrounded incident can exist.
        assert db.scalars(select(Incident)).all() == []
    finally:
        db.close()


def test_candidate_incident_resolves_when_session_returns_active(client):
    """A CANDIDATE incident resolves once its own session is ACTIVE again."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=1, questions=1)
    node_id = ids["node_ids"][0]
    session_id = ids["session_ids"][0]

    fail_node(tc, node_id)
    open_inc = by_severity(evaluate(tc, ids["exam_id"]), "CANDIDATE")
    assert len(open_inc) == 1
    assert open_inc[0]["status"] == "ACTIVE"
    assert open_inc[0]["resolved_at"] is None

    recover_node(tc, node_id)
    reconcile(tc, session_id, ids["question_ids"][0], "buf-1")

    resolved = {i["id"]: i for i in evaluate(tc, ids["exam_id"])}
    cand = resolved[open_inc[0]["id"]]
    assert cand["status"] == "RESOLVED"
    assert cand["resolved_at"] is not None
    assert cand["detected_at"] <= cand["resolved_at"]


def test_node_and_system_incidents_resolve_after_full_recovery(client):
    """NODE and SYSTEM incidents resolve only when node + every linked session clear."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    node_id = ids["node_ids"][0]
    sessions = ids["session_ids"]
    qid = ids["question_ids"][0]

    fail_node(tc, node_id)
    opened = evaluate(tc, ids["exam_id"])
    node_inc = by_severity(opened, "NODE")[0]
    sys_inc = by_severity(opened, "SYSTEM")[0]
    assert node_inc["status"] == "ACTIVE"
    assert sys_inc["status"] == "ACTIVE"

    recover_node(tc, node_id)
    for index, sid in enumerate(sessions):
        reconcile(tc, sid, qid, f"buf-{index}")

    after = {i["id"]: i for i in evaluate(tc, ids["exam_id"])}
    assert after[node_inc["id"]]["status"] == "RESOLVED"
    assert after[sys_inc["id"]]["status"] == "RESOLVED"
    for cand in by_severity(opened, "CANDIDATE"):
        assert after[cand["id"]]["status"] == "RESOLVED"


def test_incident_is_not_reopened_after_resolution(client):
    """A resolved incident stays resolved; the recovered exam opens nothing new."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=1, questions=1)
    node_id = ids["node_ids"][0]

    fail_node(tc, node_id)
    opened = {i["id"] for i in evaluate(tc, ids["exam_id"])}
    assert len(opened) == 2  # CANDIDATE + SYSTEM (1/1 sessions affected)

    recover_node(tc, node_id)
    reconcile(tc, ids["session_ids"][0], ids["question_ids"][0], "buf-x")
    resolved = {i["id"]: i for i in evaluate(tc, ids["exam_id"])}
    assert set(resolved) == opened
    assert all(i["status"] == "RESOLVED" for i in resolved.values())

    final = {i["id"]: i for i in evaluate(tc, ids["exam_id"])}
    assert set(final) == opened
    assert all(i["status"] == "RESOLVED" for i in final.values())


def test_incidents_are_strictly_scoped_per_exam(client):
    """Failures in one exam never leak incidents into another exam."""
    tc, factory = client
    exam_a = seed(factory, node_count=1, sessions_per_node=3, title="A")
    exam_b = seed(factory, node_count=1, sessions_per_node=3, title="B")

    fail_node(tc, exam_a["node_ids"][0])

    incidents_a = evaluate(tc, exam_a["exam_id"])
    assert incidents_a
    assert all(i["exam_id"] == exam_a["exam_id"] for i in incidents_a)

    assert evaluate(tc, exam_b["exam_id"]) == []

    db = factory()
    try:
        leaked = db.scalars(
            select(Incident).where(Incident.exam_id == exam_b["exam_id"])
        ).all()
        assert leaked == []
    finally:
        db.close()


def test_evidence_is_bounded_by_the_incident_episode(client):
    """Evidence starts at created_from_event_id and stops at resolved_at."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    node_id = ids["node_ids"][0]
    sessions = ids["session_ids"]
    qid = ids["question_ids"][0]

    fail_node(tc, node_id)
    node_inc = by_severity(evaluate(tc, ids["exam_id"]), "NODE")[0]
    evidence = tc.get(f"/incident/{node_inc['id']}").json()[
        "evidence_event_sequence_nos"
    ]
    assert evidence, "failure event must always be part of its own evidence"
    assert evidence == sorted(evidence)
    assert evidence[0] == node_inc["created_from_event_id"]

    # Recovery ledger activity on the same node extends the still-open episode.
    recover_node(tc, node_id)
    for index, sid in enumerate(sessions):
        reconcile(tc, sid, qid, f"fin-{index}")

    settled = {i["id"]: i for i in evaluate(tc, ids["exam_id"])}[node_inc["id"]]
    assert settled["status"] == "RESOLVED"
    evidence = tc.get(f"/incident/{node_inc['id']}").json()[
        "evidence_event_sequence_nos"
    ]
    assert evidence[0] == node_inc["created_from_event_id"]
    assert evidence == sorted(evidence)

    db = factory()
    try:
        resolved_at = db.get(Incident, node_inc["id"]).resolved_at
        events = db.scalars(
            select(Event).where(Event.sequence_no.in_(evidence))
        ).all()
        assert events
        assert resolved_at is not None
        for ev in events:
            assert ev.event_type in incident_service.ALLOWED_EVIDENCE_EVENT_TYPES
            assert ev.exam_id == ids["exam_id"]
            assert ev.sequence_no >= node_inc["created_from_event_id"]
            assert ev.server_timestamp <= resolved_at
            assert ev.node_id == node_id or ev.session_id in sessions
    finally:
        db.close()


def test_evidence_excludes_unrelated_nodes_and_sessions(client):
    """A NODE incident never cites another node's or session's events."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=3, questions=1)
    n1, n2 = ids["node_ids"]
    s1 = ids["sessions_by_node"][0]
    s2 = ids["sessions_by_node"][1]
    qid = ids["question_ids"][0]

    fail_node(tc, n1)
    unrelated = tc.post(
        f"/session/{s2[0]}/answer",
        json={"question_id": qid, "answer": "B", "client_event_id": "unrelated-1"},
    )
    assert unrelated.status_code == 200, unrelated.text

    node_inc = by_severity(evaluate(tc, ids["exam_id"]), "NODE")[0]
    evidence = tc.get(f"/incident/{node_inc['id']}").json()[
        "evidence_event_sequence_nos"
    ]

    db = factory()
    try:
        events = db.scalars(
            select(Event).where(Event.sequence_no.in_(evidence))
        ).all()
        assert all(ev.node_id in (None, n1) for ev in events)
        assert all(ev.session_id is None or ev.session_id in s1 for ev in events)
    finally:
        db.close()


def test_system_evidence_excludes_unrelated_node_events(client):
    """SYSTEM evidence cites only linked sessions and contributing nodes.

    An unrelated node in the same exam emits node-wide events during the system
    episode; none of its sequence numbers may appear in the evidence.
    """
    tc, factory = client
    ids = seed(factory, node_count=3, sessions_per_node=20)
    n1, n2, n3 = ids["node_ids"]

    # Two failed nodes open the SYSTEM incident (origin = node 1's event).
    fail_node(tc, n1)
    fail_node(tc, n2)

    system_inc = by_severity(evaluate(tc, ids["exam_id"]), "SYSTEM")[0]

    # Node 3 is NOT part of the incident but emits a node-wide event now.
    fail_node(tc, n3)
    recover_node(tc, n3)

    db = factory()
    try:
        node3_events = [
            ev.sequence_no
            for ev in db.scalars(
                select(Event)
                .where(
                    Event.node_id == n3,
                    Event.event_type.in_(incident_service.ALLOWED_EVIDENCE_EVENT_TYPES),
                )
                .order_by(Event.sequence_no)
            ).all()
        ]
        assert len(node3_events) >= 2  # failure + recovery on the unrelated node
    finally:
        db.close()

    evidence = tc.get(f"/incident/{system_inc['id']}").json()[
        "evidence_event_sequence_nos"
    ]
    assert not set(node3_events) & set(evidence)

    # What remains is strictly the incident's own contributing scope.
    contributing_nodes = {n1, n2}
    linked = set(system_inc["affected_session_ids"])
    db = factory()
    try:
        for ev in db.scalars(
            select(Event).where(Event.sequence_no.in_(evidence))
        ).all():
            assert ev.node_id in (None, *contributing_nodes)
            assert ev.session_id is None or ev.session_id in linked
    finally:
        db.close()


def test_evaluation_never_writes_ledger_events(client):
    """The detector is a pure projection: the ledger is byte-for-byte unchanged."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3)
    fail_node(tc, ids["node_ids"][0])

    def snapshot():
        db = factory()
        try:
            return [
                (ev.sequence_no, ev.event_type, ev.previous_hash, ev.hash)
                for ev in db.scalars(
                    select(Event).order_by(Event.sequence_no)
                ).all()
            ]
        finally:
            db.close()

    before = snapshot()
    evaluate(tc, ids["exam_id"])
    evaluate(tc, ids["exam_id"])
    assert snapshot() == before


def test_evaluation_preserves_the_hash_chain(client):
    """Detection and resolution never disturb ledger integrity."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    fail_node(tc, ids["node_ids"][0])
    evaluate(tc, ids["exam_id"])
    recover_node(tc, ids["node_ids"][0])
    for index, sid in enumerate(ids["session_ids"]):
        reconcile(tc, sid, ids["question_ids"][0], f"chain-{index}")
    evaluate(tc, ids["exam_id"])

    db = factory()
    try:
        events = db.scalars(
            select(Event).order_by(Event.sequence_no)
        ).all()
        assert events
        for ev in events:
            assert verify_event(
                sequence_no=ev.sequence_no,
                exam_id=ev.exam_id,
                session_id=ev.session_id,
                candidate_id=ev.candidate_id,
                node_id=ev.node_id,
                event_type=ev.event_type,
                payload=ev.payload,
                client_event_id=ev.client_event_id,
                client_timestamp=ev.client_timestamp,
                server_timestamp=ev.server_timestamp,
                previous_hash=ev.previous_hash,
                expected_hash=ev.hash,
            )
    finally:
        db.close()

    assert tc.get("/health").status_code == 200


def test_cleared_condition_does_not_open_a_new_incident(client):
    """A historical threshold crossing that already healed opens nothing."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    node_id = ids["node_ids"][0]
    qid = ids["question_ids"][0]

    fail_node(tc, node_id)
    recover_node(tc, node_id)
    for index, sid in enumerate(ids["session_ids"]):
        reconcile(tc, sid, qid, f"heal-{index}")

    # The 3-in-60s crossing is still in the ledger, but the node is HEALTHY and
    # every session is ACTIVE, so no incident may be opened from it.
    assert evaluate(tc, ids["exam_id"]) == []

    db = factory()
    try:
        assert db.scalars(
            select(Incident).where(Incident.exam_id == ids["exam_id"])
        ).all() == []
    finally:
        db.close()


def test_active_incident_lookup_helpers(client):
    """The three deterministic lookups address the three independent scopes."""
    tc, factory = client
    ids = seed(factory, node_count=2, sessions_per_node=3)
    fail_node(tc, ids["node_ids"][0])
    evaluate(tc, ids["exam_id"])

    victim = ids["sessions_by_node"][0][0]
    db = factory()
    try:
        assert (
            incident_service.find_active_candidate_incident(
                db, ids["exam_id"], victim
            )
            is not None
        )
        assert (
            incident_service.find_active_candidate_incident(db, ids["exam_id"], 99999)
            is None
        )
        assert (
            incident_service.find_active_node_incident(
                db, ids["exam_id"], ids["node_ids"][0]
            )
            is not None
        )
        assert (
            incident_service.find_active_node_incident(
                db, ids["exam_id"], ids["node_ids"][1]
            )
            is None
        )
        assert (
            incident_service.find_active_system_incident(db, ids["exam_id"])
            is not None
        )
        assert incident_service.find_active_system_incident(db, 99999) is None
    finally:
        db.close()


def test_evaluation_rolls_back_completely_on_failure(client, monkeypatch):
    """A mid-projection failure commits nothing; the exam is left untouched."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3)
    fail_node(tc, ids["node_ids"][0])

    def boom(*args, **kwargs):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(incident_service, "link_session_to_incident", boom)
    db = factory()
    try:
        with pytest.raises(RuntimeError):
            incident_service.evaluate_incidents(db, ids["exam_id"])
        assert (
            db.scalars(
                select(Incident).where(Incident.exam_id == ids["exam_id"])
            ).all()
            == []
        )
        assert db.scalars(select(IncidentSession)).all() == []
    finally:
        db.close()
    monkeypatch.undo()

    # The engine still works once the fault is removed.
    assert evaluate(tc, ids["exam_id"])


def test_incident_listing_and_detail_endpoints(client):
    """GET /incidents filters by exam and status; /incident/{id} returns evidence."""
    tc, factory = client
    ids = seed(factory, node_count=1, sessions_per_node=3, questions=1)
    fail_node(tc, ids["node_ids"][0])
    opened = evaluate(tc, ids["exam_id"])
    assert len(opened) == 5  # 3 CANDIDATE + 1 NODE + 1 SYSTEM

    listing = tc.get("/incidents", params={"exam_id": ids["exam_id"]})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == len(opened)
    assert [i["id"] for i in body["incidents"]] == sorted(i["id"] for i in opened)
    assert all(i["status"] == "ACTIVE" for i in body["incidents"])

    active = tc.get(
        "/incidents", params={"exam_id": ids["exam_id"], "status": "ACTIVE"}
    ).json()
    assert active["total"] == len(opened)

    node_inc = by_severity(opened, "NODE")[0]
    detail = tc.get(f"/incident/{node_inc['id']}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["id"] == node_inc["id"]
    assert payload["severity"] == "NODE"
    assert payload["evidence_event_sequence_nos"]

    assert tc.get("/incident/99999").status_code == 404


def test_evaluate_requires_a_real_exam(client):
    """Evaluating an unknown exam is a 404, not a silent empty projection."""
    tc, factory = client
    r = tc.post("/demo/incidents/evaluate", json={"exam_id": 99999})
    assert r.status_code == 404, r.text
