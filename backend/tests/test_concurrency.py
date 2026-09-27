"""Adversarial concurrency and trust-hardening tests (Task 12).

Proves the single-process synchronisation actually holds:

- genuinely concurrent ``append_event`` calls, then global audit verification
- concurrent answers for the same (session, question) with different
  ``sequence_no`` values: the projection must never regress
- concurrent ``evaluate_incidents`` calls: no duplicate active incident
- ``_causal_failure_event`` never falls back to another node's failure
- empty ``evidence_refs`` is rejected by the AI grounding guard

Each concurrent test uses one SQLite file with a separate Session per thread,
which is the real contention case. Concurrency is forced with a Barrier so the
threads genuinely overlap rather than accidentally running in sequence.
"""

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.hashing import verify_event
from app.db import Base, Candidate, Event, Exam, Incident, Node, Question, Response, Session
from app.services import audit_service, incident_service
from app.services.ai_incident_service import (
    AIResponseInvalidError,
    IncidentAnalysis,
    _ground_evidence_refs,
)
from app.services.event_ledger import append_event
from app.services.response_projection import apply_answer_projection

WRONGERS = 8


@pytest.fixture()
def engine(tmp_path):
    """A file-backed SQLite DB: thread-safe enough to force real contention."""
    eng = create_engine(
        f"sqlite:///{tmp_path}/test_concurrency.db",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def run_concurrently(target, workers=WRONGERS):
    """Run ``target(index, session_factory)`` on real threads, joined."""
    barrier = threading.Barrier(workers)
    results: list = [None] * workers
    errors: list = [None] * workers

    def wrapper(index: int) -> None:
        try:
            barrier.wait(timeout=20)  # force genuine overlap
            results[index] = target(index, factory)
        except BaseException as exc:  # noqa: BLE001 - recorded for assertions
            errors[index] = exc

    threads = [threading.Thread(target=wrapper, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "worker thread did not finish (deadlock?)"
    return results, errors


def seed_exam(factory, node_count=1, sessions_per_node=1, questions=1, title="C1"):
    """One exam + HEALTHY nodes + candidates + ACTIVE sessions + questions."""
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
        sessions = []
        roll = 0
        for node in nodes:
            for _ in range(sessions_per_node):
                roll += 1
                cand = Candidate(name=f"C{roll}", roll_no=f"{title}-{exam.id}-{roll}")
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
        }
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 1. Concurrent append_event -> the chain must still verify
# --------------------------------------------------------------------------- #


def test_concurrent_appends_produce_a_valid_chain(factory):
    """Genuinely concurrent appends: no duplicate predecessor, chain verifies."""
    ids = seed_exam(factory)

    def append(index, _):
        db = factory()
        try:
            return append_event(
                db,
                event_type="HEARTBEAT",
                exam_id=ids["exam_id"],
                session_id=ids["session_ids"][0],
                node_id=ids["node_ids"][0],
                payload={"worker": index},
                client_event_id=f"concurrent-{index}",
            )
        finally:
            db.close()

    results, errors = run_concurrently(append)
    assert errors == [None] * WRONGERS, f"worker raised: {[e for e in errors if e]}"

    appended = [r.event for r in results if r.appended]
    assert len(appended) == WRONGERS

    db = factory()
    try:
        # 1. every sequence number is unique and contiguous from 1.
        events = db.scalars(select(Event).order_by(Event.sequence_no.asc())).all()
        assert [e.sequence_no for e in events] == list(range(1, WRONGERS + 1))

        # 2. every event independently re-verifies against its stored hash.
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
            ) is True

        # 3. the links form one unbroken chain: no two events share a predecessor.
        links = [e.previous_hash for e in events]
        assert len(links) == len(set(links)), "two events claimed the same predecessor"
        for previous, current in zip(events, events[1:]):
            assert current.previous_hash == previous.hash

        # 4. the production audit service agrees.
        report = audit_service.verify_ledger_chain(db)
        assert report.valid is True
        assert report.first_broken_sequence_no is None
        assert report.events_checked == WRONGERS
    finally:
        db.close()


def test_concurrent_appends_preserve_genesis_and_idempotency(factory):
    """The first append still anchors to genesis; retries stay idempotent."""
    ids = seed_exam(factory)

    def append(index, _):
        db = factory()
        try:
            first = append_event(
                db,
                event_type="HEARTBEAT",
                exam_id=ids["exam_id"],
                session_id=ids["session_ids"][0],
                payload={"worker": index},
                client_event_id="shared-id",  # same id on purpose
            )
            retry = append_event(
                db,
                event_type="HEARTBEAT",
                exam_id=ids["exam_id"],
                session_id=ids["session_ids"][0],
                payload={"worker": index},
                client_event_id="shared-id",
            )
            return first.appended, retry.appended, retry.event.sequence_no
        finally:
            db.close()

    _, errors = run_concurrently(append)
    assert errors == [None] * WRONGERS, f"worker raised: {[e for e in errors if e]}"

    db = factory()
    try:
        count = db.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.client_event_id == "shared-id")
        )
        assert count == 1, "idempotency must collapse concurrent duplicates to one row"
        events = db.scalars(select(Event).order_by(Event.sequence_no.asc())).all()
        assert len(events) == 1
        assert events[0].previous_hash is not None
        assert audit_service.verify_ledger_chain(db).valid is True
    finally:
        db.close()



# --------------------------------------------------------------------------- #
# 2. Concurrent Response projection must never regress
# --------------------------------------------------------------------------- #


def test_concurrent_projection_never_regresses(factory):
    """Different sequence_nos for one (session, question): newest must win.

    Each worker writes a distinct, increasing ``sequence_no``. Whatever the
    interleaving, the stored ``last_event_id`` must end up at the maximum.
    """
    ids = seed_exam(factory)
    session_id = ids["session_ids"][0]
    question_id = ids["question_ids"][0]

    def project(index, _):
        db = factory()
        try:
            sequence_no = 100 + index
            return apply_answer_projection(
                db,
                session_id=session_id,
                question_id=question_id,
                answer=f"ans-{index}",
                sequence_no=sequence_no,
            )
        finally:
            db.close()

    _, errors = run_concurrently(project)
    assert errors == [None] * WRONGERS, f"worker raised: {[e for e in errors if e]}"

    db = factory()
    try:
        rows = db.scalars(
            select(Response).where(
                Response.session_id == session_id,
                Response.question_id == question_id,
            )
        ).all()
        assert len(rows) == 1, "UNIQUE(session_id, question_id) must hold"
        best = max(100 + i for i in range(WRONGERS))
        assert rows[0].last_event_id == best, "projection regressed below the maximum"
        assert rows[0].current_answer == f"ans-{best - 100}"
    finally:
        db.close()


def test_concurrent_projection_stale_writer_cannot_overwrite_newer(factory):
    """A late, older sequence_no must be refused, not applied."""
    ids = seed_exam(factory)
    session_id = ids["session_ids"][0]
    question_id = ids["question_ids"][0]

    db = factory()
    try:
        # Establish a clearly newer projection first.
        apply_answer_projection(
            db,
            session_id=session_id,
            question_id=question_id,
            answer="newest",
            sequence_no=500,
        )
    finally:
        db.close()

    def stale(index, _):
        db = factory()
        try:
            return apply_answer_projection(
                db,
                session_id=session_id,
                question_id=question_id,
                answer=f"stale-{index}",
                sequence_no=10 + index,  # all far behind 500
            )
        finally:
            db.close()

    results, errors = run_concurrently(stale)
    assert errors == [None] * WRONGERS, f"worker raised: {[e for e in errors if e]}"
    # Each stale caller is still told the stored projection is at/ahead of the
    # sequence_no it submitted (the documented "confirmed up-to-date" result),
    # even though its own write was correctly refused.
    assert results == [True] * WRONGERS

    db = factory()
    try:
        row = db.execute(
            select(Response).where(
                Response.session_id == session_id,
                Response.question_id == question_id,
            )
        ).scalar_one()
        # The real assertion: no stale writer mutated anything.
        assert row.last_event_id == 500
        assert row.current_answer == "newest"
    finally:
        db.close()


def test_concurrent_initial_projection_insert_creates_one_row(factory):
    """Concurrent first writes for one key yield exactly one Response row."""
    ids = seed_exam(factory)
    session_id = ids["session_ids"][0]
    question_id = ids["question_ids"][0]

    def project(index, _):
        db = factory()
        try:
            return apply_answer_projection(
                db,
                session_id=session_id,
                question_id=question_id,
                answer=f"first-{index}",
                sequence_no=1 + index,
            )
        finally:
            db.close()

    _, errors = run_concurrently(project)
    assert errors == [None] * WRONGERS, f"worker raised: {[e for e in errors if e]}"

    db = factory()
    try:
        count = db.scalar(
            select(func.count())
            .select_from(Response)
            .where(
                Response.session_id == session_id,
                Response.question_id == question_id,
            )
        )
        assert count == 1
        row = db.execute(
            select(Response).where(
                Response.session_id == session_id,
                Response.question_id == question_id,
            )
        ).scalar_one()
        assert row.last_event_id == 1 + (WRONGERS - 1)
    finally:
        db.close()



# --------------------------------------------------------------------------- #
# 3. Concurrent incident evaluation must not duplicate incidents
# --------------------------------------------------------------------------- #


def _disconnect_sessions(factory, ids):
    """Flip every session to DISCONNECTED and fail the node, with a real event."""
    db = factory()
    try:
        for session_id in ids["session_ids"]:
            db.get(Session, session_id).status = "DISCONNECTED"
        for node_id in ids["node_ids"]:
            db.get(Node, node_id).status = "FAILED"
        db.commit()
        return append_event(
            db,
            event_type="NODE_FAILURE_INJECTED",
            exam_id=ids["exam_id"],
            node_id=ids["node_ids"][0],
            payload={
                "simulation": True,
                "affected_session_ids": list(ids["session_ids"]),
            },
        )
    finally:
        db.close()


def test_concurrent_evaluation_creates_no_duplicate_active_incident(factory):
    """N simultaneous evaluations of one exam yield one active incident per scope."""
    ids = seed_exam(factory, sessions_per_node=3)
    _disconnect_sessions(factory, ids)

    def evaluate(_index, _factory):
        db = factory()
        try:
            rows = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
            return [(r.severity, r.status, r.id) for r in rows]
        finally:
            db.close()

    results, errors = run_concurrently(evaluate, workers=4)
    assert errors == [None] * 4, f"worker raised: {[e for e in errors if e]}"

    db = factory()
    try:
        # Exactly one NODE incident: the scope identity is (exam, origin node).
        active_node = db.scalars(
            select(Incident).where(
                Incident.exam_id == ids["exam_id"],
                Incident.severity == incident_service.SEVERITY_NODE,
                Incident.status == incident_service.STATUS_ACTIVE,
            )
        ).all()
        assert len(active_node) == 1, (
            f"expected exactly one active NODE incident, got {len(active_node)}"
        )

        # Exactly one SYSTEM incident per exam.
        active_system = db.scalars(
            select(Incident).where(
                Incident.exam_id == ids["exam_id"],
                Incident.severity == incident_service.SEVERITY_SYSTEM,
                Incident.status == incident_service.STATUS_ACTIVE,
            )
        ).all()
        assert len(active_system) == 1

        # One CANDIDATE incident per affected session, never duplicated.
        active_candidate = db.scalars(
            select(Incident).where(
                Incident.exam_id == ids["exam_id"],
                Incident.severity == incident_service.SEVERITY_CANDIDATE,
                Incident.status == incident_service.STATUS_ACTIVE,
            )
        ).all()
        assert len(active_candidate) == len(ids["session_ids"])
        origins = [inc.created_from_event_id for inc in active_candidate]
        assert len(set(origins)) == 1, "all candidates share one causal event"

        # No duplicates at all in the table.
        total = db.scalar(select(func.count()).select_from(Incident))
        assert total == len(active_candidate) + 1 + 1
    finally:
        db.close()



# --------------------------------------------------------------------------- #
# 4. _causal_failure_event must never borrow another node's failure
# --------------------------------------------------------------------------- #


def test_causal_failure_event_returns_none_for_wrong_node(factory):
    """A failure on a different node is not a cause for this session."""
    ids = seed_exam(factory, node_count=2, sessions_per_node=1)
    db = factory()
    try:
        session = db.get(Session, ids["session_ids"][0])
        other_node_id = next(n for n in ids["node_ids"] if n != session.node_id)
        other_failure = append_event(
            db,
            event_type="NODE_FAILURE_INJECTED",
            exam_id=ids["exam_id"],
            node_id=other_node_id,
            payload={"simulation": True, "affected_session_ids": [999999]},
        )
        assert other_failure.event.node_id != session.node_id
        events = db.scalars(select(Event).order_by(Event.sequence_no.asc())).all()

        found = incident_service._causal_failure_event(list(events), session)
        assert found is None, (
            "must not fall back to an unrelated node's NODE_FAILURE_INJECTED; "
            f"got sequence_no={getattr(found, 'sequence_no', None)}"
        )
    finally:
        db.close()


def test_causal_failure_event_matches_the_sessions_own_node(factory):
    """The happy path still returns this session's own node's failure."""
    ids = seed_exam(factory, node_count=2, sessions_per_node=1)
    db = factory()
    try:
        session = db.get(Session, ids["session_ids"][0])
        mine = append_event(
            db,
            event_type="NODE_FAILURE_INJECTED",
            exam_id=ids["exam_id"],
            node_id=session.node_id,
            payload={"simulation": True, "affected_session_ids": [session.id]},
        )
        other_node_id = next(n for n in ids["node_ids"] if n != session.node_id)
        append_event(
            db,
            event_type="NODE_FAILURE_INJECTED",
            exam_id=ids["exam_id"],
            node_id=other_node_id,
            payload={"simulation": True, "affected_session_ids": []},
        )
        events = db.scalars(select(Event).order_by(Event.sequence_no.asc())).all()

        found = incident_service._causal_failure_event(list(events), session)
        assert found is not None
        assert found.node_id == session.node_id
        assert found.sequence_no == mine.event.sequence_no
    finally:
        db.close()


def test_no_candidate_incident_when_only_another_node_failed(factory):
    """A disconnected session with no causal failure creates no incident."""
    ids = seed_exam(factory, node_count=2, sessions_per_node=1)
    db = factory()
    try:
        other_node_id = ids["node_ids"][1]
        db.get(Session, ids["session_ids"][0]).status = "DISCONNECTED"
        db.get(Node, other_node_id).status = "FAILED"
        db.commit()
        append_event(
            db,
            event_type="NODE_FAILURE_INJECTED",
            exam_id=ids["exam_id"],
            node_id=other_node_id,
            payload={"simulation": True, "affected_session_ids": [999999]},
        )

        incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        candidates = db.scalars(
            select(Incident).where(
                Incident.severity == incident_service.SEVERITY_CANDIDATE
            )
        ).all()
        assert candidates == [], (
            "an ungrounded CANDIDATE incident must not be created from another "
            "node's failure event"
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 5. NODE threshold accumulated across several failure events in the window
# --------------------------------------------------------------------------- #


def _fail_each_session_individually(factory, ids):
    """One NODE_FAILURE_INJECTED per session; no single event hits the threshold."""
    db = factory()
    try:
        for session_id in ids["session_ids"]:
            db.get(Session, session_id).status = "DISCONNECTED"
        for node_id in ids["node_ids"]:
            db.get(Node, node_id).status = "FAILED"
        db.commit()
        for session_id in ids["session_ids"]:
            append_event(
                db,
                event_type="NODE_FAILURE_INJECTED",
                exam_id=ids["exam_id"],
                node_id=ids["node_ids"][0],
                payload={"simulation": True, "affected_session_ids": [session_id]},
            )
    finally:
        db.close()


def test_node_threshold_accumulates_across_multiple_failure_events(factory):
    """Three sessions arriving via three separate failure events cross the bar."""
    ids = seed_exam(factory, node_count=1, sessions_per_node=3)
    _fail_each_session_individually(factory, ids)

    db = factory()
    try:
        incidents = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        node_incidents = [
            i for i in incidents if i.severity == incident_service.SEVERITY_NODE
        ]
        assert len(node_incidents) == 1, (
            "the accumulated threshold must open exactly one NODE incident"
        )
        node_incident = node_incidents[0]

        # Anchored to the exact crossing event (the 3rd failure), not the first.
        failure_seqs = [
            ev.sequence_no
            for ev in db.scalars(
                select(Event)
                .where(Event.event_type == "NODE_FAILURE_INJECTED")
                .order_by(Event.sequence_no.asc())
            ).all()
        ]
        assert len(failure_seqs) == 3
        assert node_incident.created_from_event_id == failure_seqs[-1]
        assert audit_service.verify_ledger_chain(db).valid is True
    finally:
        db.close()


def test_node_threshold_not_reached_with_two_sessions(factory):
    """Below the threshold, accumulated failure events open no NODE incident."""
    ids = seed_exam(factory, node_count=1, sessions_per_node=2)
    _fail_each_session_individually(factory, ids)

    db = factory()
    try:
        incidents = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        assert [
            i for i in incidents if i.severity == incident_service.SEVERITY_NODE
        ] == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6. AI grounding: empty evidence_refs is rejected
# --------------------------------------------------------------------------- #


def _analysis(refs):
    return IncidentAnalysis(
        likely_cause="c",
        impact_summary="i",
        recommended_response="r",
        evidence_refs=list(refs),
    )


def test_empty_evidence_refs_is_rejected():
    """An analysis citing nothing is not grounded and must be refused."""
    with pytest.raises(AIResponseInvalidError) as excinfo:
        _ground_evidence_refs(_analysis([]), [1, 2, 3])
    assert "no evidence_refs" in str(excinfo.value)


def test_ungrounded_evidence_refs_still_rejected():
    """The existing strict reference check is unchanged."""
    with pytest.raises(AIResponseInvalidError) as excinfo:
        _ground_evidence_refs(_analysis([1, 4242]), [1, 2, 3])
    assert "ungrounded" in str(excinfo.value)
    assert "4242" in str(excinfo.value)


def test_grounded_evidence_refs_are_accepted_and_sorted():
    """Real references survive, de-duplicated and ordered."""
    assert _ground_evidence_refs(_analysis([3, 1, 3]), [1, 2, 3]) == [1, 3]


def test_evidence_refs_must_exist_even_when_package_is_empty():
    """No allowed references at all means any citation is ungrounded."""
    with pytest.raises(AIResponseInvalidError):
        _ground_evidence_refs(_analysis([7]), [])


def test_repeated_sequential_evaluation_is_still_idempotent(factory):
    """Sequential re-evaluation reuses incidents and never duplicates them."""
    ids = seed_exam(factory, sessions_per_node=3)
    _disconnect_sessions(factory, ids)

    db = factory()
    try:
        first = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        second = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        third = incident_service.evaluate_incidents(db, exam_id=ids["exam_id"])
        assert {i.id for i in first} == {i.id for i in second} == {i.id for i in third}
        total = db.scalar(select(func.count()).select_from(Incident))
        assert total == len(first)
    finally:
        db.close()
