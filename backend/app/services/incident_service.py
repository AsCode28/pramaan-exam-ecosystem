"""Incident detection, escalation, and resolution engine (Task 6).

``evaluate_incidents()`` is a deterministic operational projection over three
read-only inputs: the append-only Event ledger, the current Session states, and
the current Node states.

Scope hierarchy (three independent scopes, never merged in place)::

    CANDIDATE  ->  (exam_id, session_id)          one row per affected session
    NODE       ->  (exam_id, origin_event.node_id) one row per affected node
    SYSTEM     ->  (exam_id)                      one row per exam

NODE identity is resolved by joining ``Incident.created_from_event_id`` to
``Event.sequence_no`` -- no extra column and no denormalised node_id on the
incident row.

Thresholds
----------
- NODE: >= 3 affected sessions on one node within a rolling 60s window, derived
  from real ``NODE_FAILURE_INJECTED`` events (table counts alone can never open
  a NODE incident). ``created_from_event_id`` is the exact threshold-crossing
  event, not the first failure of the node.
- SYSTEM: >= 2 FAILED nodes **or** strictly more than 15% affected sessions
  (DISCONNECTED + RECOVERING).

Active-condition guard
---------------------
A historical threshold crossing never opens a NEW incident if the operational
condition has already cleared. A NEW NODE incident additionally requires the
node to be FAILED right now: a HEALTHY node whose sessions are still affected
or recovering may only REUSE an already-open NODE incident, never create one.
An already-open incident keeps being reused until it resolves.

Grounding
---------
A CANDIDATE incident is created only when a causal ``NODE_FAILURE_INJECTED``
event exists, so every persisted CANDIDATE incident carries a real
``created_from_event_id``. SYSTEM evidence is limited to the incident's
contributing nodes (origin event node + nodes of linked sessions); a node is
never treated as contributing just because it emitted an event during the
episode.

Resolution
----------
CANDIDATE -> its own session is ACTIVE.
NODE      -> origin node is HEALTHY **and** every linked session is ACTIVE.
SYSTEM    -> < 2 failed nodes, affected ratio <= 15%, and every linked session
             is ACTIVE.

Integrity rules
---------------
- Pure projection: this module never calls ``append_event()`` and never writes
  Event rows, so evaluation cannot disturb the hash chain.
- Evidence is restricted to real event types (``ALLOWED_EVIDENCE_EVENT_TYPES``)
  inside the episode window ``[created_from_event_id, resolved_at or now]`` and
  strictly scoped to the entities that contribute to that incident.
- Ordering and windowing use ``server_timestamp`` / ``sequence_no`` only;
  ``client_timestamp`` is never trusted.
- The whole evaluation runs in ONE transaction: single commit, full rollback on
  any failure.
- Concurrent evaluation of the same exam is serialised per exam by an
  in-process lock, so two simultaneous evaluations cannot both create a
  duplicate active incident. This is single-process protection, not distributed
  locking.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import NotFound
from app.db.event import Event
from app.db.exam import Exam, Node
from app.db.incident import Incident, IncidentSession
from app.db.session import Session

# Per-exam evaluation locks: {exam_id: Lock}. Two concurrent evaluations of the
# same exam would both see "no active incident" and both create a row. Keying by
# exam keeps unrelated exams evaluating concurrently. Single-process only.
_EVALUATION_LOCKS: dict[int, threading.Lock] = {}
_EVALUATION_LOCKS_GUARD = threading.Lock()


def _evaluation_lock(exam_id: int) -> threading.Lock:
    """Return the stable per-exam evaluation lock, creating it once."""
    with _EVALUATION_LOCKS_GUARD:
        lock = _EVALUATION_LOCKS.get(exam_id)
        if lock is None:
            lock = threading.Lock()
            _EVALUATION_LOCKS[exam_id] = lock
        return lock

# Severities, ordered from narrowest to broadest scope.
SEVERITY_CANDIDATE = "CANDIDATE"
SEVERITY_NODE = "NODE"
SEVERITY_SYSTEM = "SYSTEM"

STATUS_ACTIVE = "ACTIVE"
STATUS_RESOLVED = "RESOLVED"

# Session states that count as "affected" for every threshold and resolution.
AFFECTED_SESSION_STATES = ("DISCONNECTED", "RECOVERING")

# Thresholds.
NODE_AFFECTED_THRESHOLD = 3
NODE_WINDOW_SECONDS = 60
SYSTEM_FAILED_NODES_THRESHOLD = 2
SYSTEM_AFFECTED_PERCENT_THRESHOLD = 0.15

# The only real event types that may be cited as incident evidence.
ALLOWED_EVIDENCE_EVENT_TYPES = (
    "NODE_FAILURE_INJECTED",
    "NODE_RECOVERY_INITIATED",
    "SESSION_STARTED",
    "ANSWER_SAVED",
    "SESSION_RECOVERED",
)


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone-aware DATETIME)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_active_candidate_incident(
    db: DbSession, exam_id: int, session_id: int
) -> Incident | None:
    """Find active CANDIDATE incident for (exam_id, session_id)."""
    stmt = (
        select(Incident)
        .join(IncidentSession, Incident.id == IncidentSession.incident_id)
        .where(
            Incident.exam_id == exam_id,
            Incident.severity == SEVERITY_CANDIDATE,
            Incident.status == STATUS_ACTIVE,
            IncidentSession.session_id == session_id,
        )
    )
    return db.scalars(stmt).first()


def find_active_node_incident(
    db: DbSession, exam_id: int, node_id: int
) -> Incident | None:
    """Find active NODE incident for (exam_id, origin_event.node_id)."""
    stmt = (
        select(Incident)
        .join(Event, Incident.created_from_event_id == Event.sequence_no)
        .where(
            Incident.exam_id == exam_id,
            Incident.severity == SEVERITY_NODE,
            Incident.status == STATUS_ACTIVE,
            Event.node_id == node_id,
        )
    )
    return db.scalars(stmt).first()


def find_active_system_incident(db: DbSession, exam_id: int) -> Incident | None:
    """Find active SYSTEM incident for exam_id."""
    stmt = select(Incident).where(
        Incident.exam_id == exam_id,
        Incident.severity == SEVERITY_SYSTEM,
        Incident.status == STATUS_ACTIVE,
    )
    return db.scalars(stmt).first()


def link_session_to_incident(
    db: DbSession, incident: Incident, session_id: int
) -> None:
    """Idempotently link session_id to incident via IncidentSession."""
    existing = db.scalar(
        select(IncidentSession).where(
            IncidentSession.incident_id == incident.id,
            IncidentSession.session_id == session_id,
        )
    )
    if existing is None:
        db.add(IncidentSession(incident_id=incident.id, session_id=session_id))
        # Flush immediately: callers keep querying in the same transaction and
        # the test fixtures run with autoflush=False.
        db.flush()


def _node_events_are_shared(event_type: str) -> bool:
    """True for node-wide event types that are attributable to a whole node."""
    return event_type in ("NODE_FAILURE_INJECTED", "NODE_RECOVERY_INITIATED")


def get_incident_evidence(db: DbSession, incident: Incident) -> list[int]:
    """Return the ordered sequence numbers that ground this incident.

    Every candidate event must satisfy all of:
    - ``exam_id`` matches the incident,
    - ``sequence_no >= incident.created_from_event_id`` (episode start),
    - ``server_timestamp <= resolved_at or now`` (episode end),
    - ``event_type`` is one of ``ALLOWED_EVIDENCE_EVENT_TYPES``,
    - the event is attributable to this incident's own entities.

    Attribution is per severity: CANDIDATE sees only its own session (plus
    node-wide events on that session's node), NODE sees only its origin node
    and its linked sessions, SYSTEM sees only the nodes and sessions that
    actually contribute to it.
    """
    if incident.created_from_event_id is None:
        return []

    linked_session_ids = set(
        db.scalars(
            select(IncidentSession.session_id).where(
                IncidentSession.incident_id == incident.id
            )
        ).all()
    )
    upper_ts = incident.resolved_at or utcnow()

    events = db.scalars(
        select(Event)
        .where(
            Event.exam_id == incident.exam_id,
            Event.sequence_no >= incident.created_from_event_id,
            Event.server_timestamp <= upper_ts,
            Event.event_type.in_(ALLOWED_EVIDENCE_EVENT_TYPES),
        )
        .order_by(Event.sequence_no.asc())
    ).all()

    origin_event = db.get(Event, incident.created_from_event_id)
    origin_node_id = origin_event.node_id if origin_event else None

    if incident.severity == SEVERITY_CANDIDATE:
        target_session_id = next(iter(sorted(linked_session_ids)), None)
        target_session = (
            db.get(Session, target_session_id) if target_session_id else None
        )
        target_node_id = target_session.node_id if target_session else None
        return [
            ev.sequence_no
            for ev in events
            if ev.session_id == target_session_id
            or (
                target_node_id is not None
                and ev.node_id == target_node_id
                and _node_events_are_shared(ev.event_type)
            )
        ]

    if incident.severity == SEVERITY_NODE:
        return [
            ev.sequence_no
            for ev in events
            if (
                origin_node_id is not None
                and ev.node_id == origin_node_id
                and (
                    ev.session_id is None
                    or ev.session_id in linked_session_ids
                    # Session events of unlinked sessions on the same node are
                    # that session's business, not this node incident's.
                    or _node_events_are_shared(ev.event_type)
                )
            )
            or (ev.session_id is not None and ev.session_id in linked_session_ids)
        ]

    if incident.severity == SEVERITY_SYSTEM:
        # Contributing nodes are ONLY the origin node and the nodes of the
        # sessions linked to THIS system incident. A node is never inferred as
        # contributing just because it emitted an event during the episode.
        contributing_node_ids = set()
        if origin_node_id is not None:
            contributing_node_ids.add(origin_node_id)
        if linked_session_ids:
            contributing_node_ids.update(
                db.scalars(
                    select(Session.node_id).where(Session.id.in_(linked_session_ids))
                ).all()
            )
        return [
            ev.sequence_no
            for ev in events
            if (ev.session_id is not None and ev.session_id in linked_session_ids)
            or (ev.session_id is None and ev.node_id in contributing_node_ids)
        ]

    return []


def evaluate_incidents(db: DbSession, exam_id: int) -> list[Incident]:
    """Project, escalate and resolve every incident of one exam.

    Runs entirely inside ONE transaction: every incident row, every link and
    every status change either lands together or not at all. Read-only with
    respect to the Event ledger.

    Returns every incident of the exam (ACTIVE and RESOLVED), ordered by id.

    Concurrency: the whole evaluation is serialised per exam by
    :data:`_EVALUATION_LOCKS`. Two concurrent evaluations of the same exam
    would otherwise both observe "no active incident" and each create a
    duplicate row. This is **single-process** protection only.
    """
    if db.get(Exam, exam_id) is None:
        raise NotFound(f"Exam {exam_id} does not exist")

    with _evaluation_lock(exam_id):
        return _evaluate_incidents_locked(db, exam_id)


def _evaluate_incidents_locked(db: DbSession, exam_id: int) -> list[Incident]:
    """Evaluate one exam. Caller must already hold the per-exam lock."""
    now = utcnow()
    window_start = now - timedelta(seconds=NODE_WINDOW_SECONDS)

    try:
        all_sessions = db.scalars(
            select(Session).where(Session.exam_id == exam_id)
        ).all()
        total_sessions = len(all_sessions)
        known_session_ids = {s.id for s in all_sessions}

        affected_sessions = [
            s for s in all_sessions if s.status in AFFECTED_SESSION_STATES
        ]
        affected_session_ids = [s.id for s in affected_sessions]

        failed_node_ids = set(
            db.scalars(
                select(Node.id).where(
                    Node.exam_id == exam_id, Node.status == "FAILED"
                )
            ).all()
        )

        # Authoritative ordering is sequence_no; the rolling window is measured
        # on server_timestamp. client_timestamp is never used.
        failure_events = db.scalars(
            select(Event)
            .where(
                Event.exam_id == exam_id,
                Event.event_type == "NODE_FAILURE_INJECTED",
            )
            .order_by(Event.sequence_no.asc())
        ).all()
        recent_failure_events = [
            ev for ev in failure_events if ev.server_timestamp >= window_start
        ]

        # ---------------------------------------------------------- CANDIDATE
        # One independent row per affected session, keyed (exam_id, session_id).
        for session in affected_sessions:
            if find_active_candidate_incident(db, exam_id, session.id) is not None:
                continue
            origin_event = _causal_failure_event(failure_events, session)
            if origin_event is None:
                # Every persisted incident must be grounded in a real event.
                # An affected session with no causal NODE_FAILURE_INJECTED is
                # anomalous and must not produce an ungrounded incident row.
                continue
            incident = Incident(
                exam_id=exam_id,
                severity=SEVERITY_CANDIDATE,
                status=STATUS_ACTIVE,
                root_cause_summary=(
                    f"Session {session.id} disrupted on node {session.node_id}"
                ),
                created_from_event_id=origin_event.sequence_no,
                detected_at=now,
                resolved_at=None,
            )
            db.add(incident)
            db.flush()
            link_session_to_incident(db, incident, session.id)

        # ---------------------------------------------------------------- NODE
        # Event-derived threshold: >= 3 affected sessions on the same node
        # inside a rolling 60s window of real NODE_FAILURE_INJECTED events.
        candidate_node_ids = {s.node_id for s in affected_sessions} | failed_node_ids
        for node_id in sorted(candidate_node_ids):
            node_session_ids = [
                s.id for s in affected_sessions if s.node_id == node_id
            ]
            active_node = find_active_node_incident(db, exam_id, node_id)

            if active_node is not None:
                # Reuse the open episode and widen its blast radius.
                for session_id in node_session_ids:
                    link_session_to_incident(db, active_node, session_id)
                active_node.root_cause_summary = (
                    f"Node {node_id} outage affecting "
                    f"{len(node_session_ids)} sessions"
                )
                continue

            # ACTIVE-CONDITION GUARD: a brand-new NODE incident may only be
            # opened while the node is FAILED right now. A healthy node whose
            # sessions are still affected/recovering may only REUSE the open
            # incident above; it must never open a new one from historical
            # threshold evidence.
            node = db.get(Node, node_id)
            if node is None or node.status != "FAILED":
                continue

            crossing_event, crossed_sessions = _node_threshold_crossing(
                recent_failure_events, node_id
            )
            if crossing_event is None:
                continue

            incident = Incident(
                exam_id=exam_id,
                severity=SEVERITY_NODE,
                status=STATUS_ACTIVE,
                root_cause_summary=(
                    f"Node {node_id} outage reached {len(crossed_sessions)} "
                    f"affected sessions within {NODE_WINDOW_SECONDS}s"
                ),
                created_from_event_id=crossing_event.sequence_no,
                detected_at=now,
                resolved_at=None,
            )
            db.add(incident)
            db.flush()
            for session_id in set(node_session_ids) | (
                crossed_sessions & known_session_ids
            ):
                link_session_to_incident(db, incident, session_id)

        # -------------------------------------------------------------- SYSTEM
        affected_ratio = (
            len(affected_sessions) / total_sessions if total_sessions else 0.0
        )
        system_condition_active = (
            len(failed_node_ids) >= SYSTEM_FAILED_NODES_THRESHOLD
            or (
                total_sessions > 0
                and affected_ratio > SYSTEM_AFFECTED_PERCENT_THRESHOLD
            )
        )
        system_summary = (
            f"{len(failed_node_ids)} failed node(s); "
            f"{len(affected_sessions)}/{total_sessions} sessions affected "
            f"({affected_ratio:.1%})"
        )

        active_system = find_active_system_incident(db, exam_id)
        if active_system is not None:
            for session_id in affected_session_ids:
                link_session_to_incident(db, active_system, session_id)
            active_system.root_cause_summary = f"System-wide outage: {system_summary}"
        elif system_condition_active:
            # The CURRENT condition is active, so the incident must be anchored
            # to the first real event that explains it. If no event can explain
            # the current condition, the projection is left unchanged rather
            # than persisting an ungrounded SYSTEM incident.
            origin_sequence_no = _system_threshold_crossing(
                failure_events,
                current_failed_node_ids=failed_node_ids,
                current_affected_session_ids=set(affected_session_ids),
                total_sessions=total_sessions,
            )
            if origin_sequence_no is not None:
                incident = Incident(
                    exam_id=exam_id,
                    severity=SEVERITY_SYSTEM,
                    status=STATUS_ACTIVE,
                    root_cause_summary=f"System-wide outage: {system_summary}",
                    created_from_event_id=origin_sequence_no,
                    detected_at=now,
                    resolved_at=None,
                )
                db.add(incident)
                db.flush()
                for session_id in affected_session_ids:
                    link_session_to_incident(db, incident, session_id)

        # ---------------------------------------------------------- RESOLUTION
        for incident in db.scalars(
            select(Incident).where(
                Incident.exam_id == exam_id, Incident.status == STATUS_ACTIVE
            )
        ).all():
            summary = _resolution_summary(db, incident, total_sessions)
            if summary is not None:
                incident.status = STATUS_RESOLVED
                incident.resolved_at = now
                incident.root_cause_summary = summary

        db.commit()
        return db.scalars(
            select(Incident)
            .where(Incident.exam_id == exam_id)
            .order_by(Incident.id.asc())
        ).all()
    except Exception:
        db.rollback()
        raise


def _causal_failure_event(
    failure_events: list[Event], session: Session
) -> Event | None:
    """Most recent NODE_FAILURE_INJECTED that disrupted this exact session.

    Returns ``None`` when no failure event belongs to the session's own node.
    It must never fall back to another node's failure: a node that never
    touched this session cannot be the cause of this session's disconnection,
    and anchoring a CANDIDATE incident to such an event would ground it in
    unrelated evidence.
    """
    for event in reversed(failure_events):
        if event.node_id != session.node_id:
            continue
        affected = (event.payload or {}).get("affected_session_ids") or []
        if not affected or session.id in affected:
            return event
    return None


def _node_threshold_crossing(
    recent_failure_events: list[Event], node_id: int
) -> tuple[Event | None, set[int]]:
    """First event at which node_id accumulated >= 3 affected sessions in 60s.

    Returns the exact crossing event plus every session counted up to it, so the
    incident is anchored to the crossing rather than to the first failure.
    """
    counted: set[int] = set()
    for event in recent_failure_events:
        if event.node_id != node_id:
            continue
        for session_id in (event.payload or {}).get("affected_session_ids") or []:
            counted.add(session_id)
        if len(counted) >= NODE_AFFECTED_THRESHOLD:
            return event, counted
    return None, counted


def _system_threshold_crossing(
    failure_events: list[Event],
    *,
    current_failed_node_ids: set[int],
    current_affected_session_ids: set[int],
    total_sessions: int,
) -> int | None:
    """First real ``Event.sequence_no`` that explains the CURRENT system condition.

    Only the current state can satisfy a SYSTEM threshold:

    - a node counts only while it is still in ``current_failed_node_ids``
      (a node that recovered is never counted, however old its failure event),
    - a session counts only while it is still in
      ``current_affected_session_ids`` (a session that recovered is never
      counted, however old the event that affected it).

    The failure events are then walked chronologically and the first event that
    makes the *current* condition cross a threshold is returned:

    - >= ``SYSTEM_FAILED_NODES_THRESHOLD`` distinct currently-failed nodes
      represented so far, or
    - a ratio of currently-affected accumulated sessions over ``total_sessions``
      that is strictly greater than ``SYSTEM_AFFECTED_PERCENT_THRESHOLD``.

    Returns ``None`` when no event can explain the current condition; the caller
    must then leave the projection unchanged instead of anchoring an incident to
    an unrelated event.
    """
    failed_nodes: set[int] = set()
    counted_sessions: set[int] = set()
    for event in failure_events:
        if event.node_id is not None and event.node_id in current_failed_node_ids:
            failed_nodes.add(event.node_id)
        for session_id in (event.payload or {}).get("affected_session_ids") or []:
            if session_id in current_affected_session_ids:
                counted_sessions.add(session_id)
        ratio = len(counted_sessions) / total_sessions if total_sessions else 0.0
        if (
            len(failed_nodes) >= SYSTEM_FAILED_NODES_THRESHOLD
            or ratio > SYSTEM_AFFECTED_PERCENT_THRESHOLD
        ):
            return event.sequence_no
    return None


def _resolution_summary(
    db: DbSession, incident: Incident, total_sessions: int
) -> str | None:
    """Return the resolution summary, or None while the incident is still live.

    Resolution is always verified against the current Node / Session tables,
    never inferred from the incident's own history.
    """
    linked_session_ids = db.scalars(
        select(IncidentSession.session_id).where(
            IncidentSession.incident_id == incident.id
        )
    ).all()
    linked_sessions = [
        session
        for session in (db.get(Session, sid) for sid in linked_session_ids)
        if session is not None
    ]
    all_sessions_active = all(s.status == "ACTIVE" for s in linked_sessions)

    if incident.severity == SEVERITY_CANDIDATE:
        if not linked_sessions:
            return "Candidate incident closed: no session remained affected"
        if all_sessions_active:
            ids = ", ".join(str(s.id) for s in linked_sessions)
            return f"Session(s) {ids} returned to ACTIVE"
        return None

    if incident.severity == SEVERITY_NODE:
        origin_event = (
            db.get(Event, incident.created_from_event_id)
            if incident.created_from_event_id is not None
            else None
        )
        node = db.get(Node, origin_event.node_id) if origin_event else None
        if node is None or node.status != "HEALTHY" or not all_sessions_active:
            return None
        return f"Node {node.id} is HEALTHY again and all affected sessions are ACTIVE"

    if incident.severity == SEVERITY_SYSTEM:
        failed_nodes = db.scalars(
            select(Node.id).where(
                Node.exam_id == incident.exam_id, Node.status == "FAILED"
            )
        ).all()
        affected = db.scalars(
            select(Session.id).where(
                Session.exam_id == incident.exam_id,
                Session.status.in_(AFFECTED_SESSION_STATES),
            )
        ).all()
        ratio = len(affected) / total_sessions if total_sessions else 0.0
        if (
            len(failed_nodes) >= SYSTEM_FAILED_NODES_THRESHOLD
            or ratio > SYSTEM_AFFECTED_PERCENT_THRESHOLD
            or not all_sessions_active
        ):
            return None
        return "System recovered: failed-node and affected-session thresholds cleared"


def build_incident_response(db: DbSession, incident: Incident) -> dict[str, Any]:
    """Serialise an incident with its linked sessions and grounded evidence."""
    linked_session_ids = db.scalars(
        select(IncidentSession.session_id)
        .where(IncidentSession.incident_id == incident.id)
        .order_by(IncidentSession.session_id.asc())
    ).all()
    return {
        "id": incident.id,
        "exam_id": incident.exam_id,
        "severity": incident.severity,
        "status": incident.status,
        "root_cause_summary": incident.root_cause_summary,
        "created_from_event_id": incident.created_from_event_id,
        "detected_at": incident.detected_at,
        "resolved_at": incident.resolved_at,
        "affected_session_ids": list(linked_session_ids),
        "evidence_event_sequence_nos": get_incident_evidence(db, incident),
    }
