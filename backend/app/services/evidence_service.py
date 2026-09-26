"""Evidence packaging service for incidents.

Builds a structured, read-only evidence package grounding an incident:
- Reuses incident_service.get_incident_evidence(db, incident) so evidence selection
  is single-sourced and identical to the incident engine.
- Strictly bounds affected_node_ids to Task 6 attribution rules:
    * CANDIDATE: the target session's node (falling back to the origin event's
      node only when the incident has no linked session).
    * NODE: the origin node of the episode.
    * SYSTEM: contributing nodes (origin node + nodes of linked sessions).
  A node is never inferred as contributing merely because it emitted an event
  during the episode window.
- Gathers full details of all ordered evidence events.
- Extracts deterministic recovery facts that database and events actually establish.
- Includes global audit verification status from audit_service.verify_ledger_chain(db).
- Purely read-only: does not modify Event rows or database state.
"""

from __future__ import annotations

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db.event import Event
from app.db.exam import Node
from app.db.incident import Incident, IncidentSession
from app.db.session import Session
from app.services import (
    audit_service,
    failure_service,
    incident_service,
    recovery_service,
)


def get_affected_node_ids(db: DbSession, incident: Incident) -> list[int]:
    """Affected node IDs, scoped exactly as the Task 6 attribution rules.

    A node is never inferred as contributing merely because it emitted an event
    inside the episode window.
    """
    linked_session_ids = set(
        db.scalars(
            select(IncidentSession.session_id).where(
                IncidentSession.incident_id == incident.id
            )
        ).all()
    )

    origin_event = (
        db.get(Event, incident.created_from_event_id)
        if incident.created_from_event_id
        else None
    )
    origin_node_id = origin_event.node_id if origin_event else None

    if incident.severity == incident_service.SEVERITY_CANDIDATE:
        target_session_id = next(iter(sorted(linked_session_ids)), None)
        target_session = (
            db.get(Session, target_session_id) if target_session_id else None
        )
        target_node_id = target_session.node_id if target_session else None
        node_ids = {target_node_id if target_node_id is not None else origin_node_id}
        return sorted(node_id for node_id in node_ids if node_id is not None)

    if incident.severity == incident_service.SEVERITY_NODE:
        return sorted([origin_node_id] if origin_node_id is not None else [])

    if incident.severity == incident_service.SEVERITY_SYSTEM:
        # Contributing nodes are ONLY the origin node and the nodes of the
        # sessions linked to THIS incident.
        contributing_node_ids = set()
        if origin_node_id is not None:
            contributing_node_ids.add(origin_node_id)
        if linked_session_ids:
            contributing_node_ids.update(
                db.scalars(
                    select(Session.node_id).where(Session.id.in_(linked_session_ids))
                ).all()
            )
        return sorted(
            node_id for node_id in contributing_node_ids if node_id is not None
        )

    return []


def extract_recovery_facts(
    incident: Incident,
    evidence_events: list[Event],
    affected_sessions: list[Session],
    affected_nodes: list[Node],
) -> dict[str, Any]:
    """Extract deterministic facts that the database and events can establish.

    Buffered-answer reconciliation is reported ONLY from a real
    ``SESSION_RECOVERED`` event (which itself carries the reconciled
    ``client_event_id`` list). A generic ``ANSWER_SAVED`` event is never treated
    as proof that a reconciliation completed.
    """
    node_failures = []
    node_recoveries = []
    session_recovered_events = []

    for ev in evidence_events:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if ev.event_type == failure_service.NODE_FAILURE_INJECTED:
            node_failures.append(
                {
                    "sequence_no": ev.sequence_no,
                    "node_id": ev.node_id,
                    "server_timestamp": ev.server_timestamp,
                    "reason": payload.get("reason"),
                }
            )
        elif ev.event_type == recovery_service.NODE_RECOVERY_INITIATED:
            node_recoveries.append(
                {
                    "sequence_no": ev.sequence_no,
                    "node_id": ev.node_id,
                    "server_timestamp": ev.server_timestamp,
                    "reason": payload.get("reason"),
                }
            )
        elif ev.event_type == recovery_service.SESSION_RECOVERED:
            reconciled_ids = payload.get("reconciled_event_ids", []) or []
            session_recovered_events.append(
                {
                    "sequence_no": ev.sequence_no,
                    "session_id": ev.session_id,
                    "server_timestamp": ev.server_timestamp,
                    "reconciled_event_count": len(reconciled_ids),
                    "reconciled_client_event_ids": list(reconciled_ids),
                }
            )

    session_status_counts: dict[str, int] = {}
    for s in affected_sessions:
        session_status_counts[s.status] = session_status_counts.get(s.status, 0) + 1

    node_status_counts: dict[str, int] = {}
    for n in affected_nodes:
        node_status_counts[n.status] = node_status_counts.get(n.status, 0) + 1

    return {
        "node_failure_count": len(node_failures),
        "node_failures": node_failures,
        "node_recovery_count": len(node_recoveries),
        "node_recoveries": node_recoveries,
        "session_recovered_event_count": len(session_recovered_events),
        "session_recovered_events": session_recovered_events,
        "session_status_counts": session_status_counts,
        "node_status_counts": node_status_counts,
        "all_sessions_active": bool(affected_sessions)
        and all(s.status == "ACTIVE" for s in affected_sessions),
        "all_nodes_healthy": bool(affected_nodes)
        and all(n.status == "HEALTHY" for n in affected_nodes),
        "is_resolved": incident.status == incident_service.STATUS_RESOLVED,
    }


def build_evidence_package(db: DbSession, incident: Incident) -> dict[str, Any]:
    """Assemble the complete, deterministic, read-only evidence package."""
    # 1. Evidence sequence numbers from incident engine
    evidence_seq_nos = incident_service.get_incident_evidence(db, incident)

    # 2. Fetch full event rows ordered by sequence_no ASC
    evidence_events = []
    if evidence_seq_nos:
        evidence_events = db.scalars(
            select(Event)
            .where(Event.sequence_no.in_(evidence_seq_nos))
            .order_by(Event.sequence_no.asc())
        ).all()

    # 3. Affected sessions and nodes
    linked_session_ids = db.scalars(
        select(IncidentSession.session_id)
        .where(IncidentSession.incident_id == incident.id)
        .order_by(IncidentSession.session_id.asc())
    ).all()
    affected_sessions = (
        db.scalars(
            select(Session)
            .where(Session.id.in_(linked_session_ids))
            .order_by(Session.id.asc())
        ).all()
        if linked_session_ids
        else []
    )

    affected_node_ids = get_affected_node_ids(db, incident)
    affected_nodes = (
        db.scalars(
            select(Node).where(Node.id.in_(affected_node_ids)).order_by(Node.id.asc())
        ).all()
        if affected_node_ids
        else []
    )

    # 4. Deterministic recovery facts
    recovery_facts = extract_recovery_facts(
        incident,
        evidence_events,
        affected_sessions,
        affected_nodes,
    )

    # 5. Global ledger audit verification status
    audit_result = audit_service.verify_ledger_chain(db)
    audit_status = {
        "valid": audit_result.valid,
        "events_checked": audit_result.events_checked,
        "first_broken_sequence_no": audit_result.first_broken_sequence_no,
        "failure_reason": audit_result.failure_reason,
    }

    # 6. Format event details
    event_details = [
        {
            "sequence_no": ev.sequence_no,
            "event_type": ev.event_type,
            "exam_id": ev.exam_id,
            "session_id": ev.session_id,
            "candidate_id": ev.candidate_id,
            "node_id": ev.node_id,
            "payload": ev.payload,
            "server_timestamp": ev.server_timestamp,
            "previous_hash": ev.previous_hash,
            "hash": ev.hash,
        }
        for ev in evidence_events
    ]

    session_details = [
        {
            "id": s.id,
            "exam_id": s.exam_id,
            "candidate_id": s.candidate_id,
            "node_id": s.node_id,
            "status": s.status,
            "started_at": s.started_at,
            "submitted_at": s.submitted_at,
            "last_activity": s.last_activity,
        }
        for s in affected_sessions
    ]

    node_details = [
        {
            "id": n.id,
            "exam_id": n.exam_id,
            "status": n.status,
        }
        for n in affected_nodes
    ]

    incident_summary = {
        "id": incident.id,
        "exam_id": incident.exam_id,
        "severity": incident.severity,
        "status": incident.status,
        "root_cause_summary": incident.root_cause_summary,
        "created_from_event_id": incident.created_from_event_id,
        "detected_at": incident.detected_at,
        "resolved_at": incident.resolved_at,
    }

    return {
        "incident": incident_summary,
        "affected_session_ids": list(linked_session_ids),
        "affected_node_ids": affected_node_ids,
        "sessions": session_details,
        "nodes": node_details,
        "evidence_events": event_details,
        "recovery_facts": recovery_facts,
        "audit_status": audit_status,
    }
