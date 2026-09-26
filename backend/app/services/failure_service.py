"""Node-failure simulation service (demo scope only).

``fail_node()`` performs a REAL backend state transition (not a UI flag):
Node.status -> FAILED and every ACTIVE session on that node ->
DISCONNECTED, recorded in the append-only Event ledger as
NODE_FAILURE_INJECTED.

Transaction design (state-first, event-second; no append_event() change):
  validate -> flip node + sessions in memory -> flush (NO commit) ->
  append_event() (commits internally). That single commit persists the
  flips and the event together. If append_event() fails before commit, its
  rollback undoes the uncommitted flips too: node unchanged, sessions
  unchanged, no event.

Session states in Task 4: ACTIVE (writable) and DISCONNECTED (failed-node
sessions; writes rejected). Later Task 5 transition (not built here):
DISCONNECTED -> RECOVERING -> ACTIVE.

All event creation goes through append_event(); this module never writes
Event rows directly.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import InvalidNodeState, NotFound, NodeUnavailable  # noqa: F401  (re-exported for routers)
from app.db.exam import Node
from app.db.session import Session
from app.services import event_ledger
from app.services.event_ledger import AppendResult

NODE_FAILURE_INJECTED = "NODE_FAILURE_INJECTED"

FAILED = "FAILED"
ACTIVE = "ACTIVE"
DISCONNECTED = "DISCONNECTED"

# Node source statuses that may transition to FAILED.
FAILABLE_NODE_STATUSES = ("HEALTHY", "DEGRADED")


def fail_node(
    db: DbSession, *, node_id: int, reason: str | None = None
) -> tuple[Node, list[int], AppendResult, bool]:
    """Fail a node: flip state, disconnect ACTIVE sessions, ledger the event.

    Allowed transitions: HEALTHY -> FAILED and DEGRADED -> FAILED only.
    An already-FAILED node is an idempotent repeat (no state changes, no
    second event; newly_failed=False). Any other node status raises
    InvalidNodeState and changes nothing.

    Returns (node, affected_session_ids, result, newly_failed). For an
    idempotent repeat the affected list comes from the ORIGINAL event's
    immutable payload, so the response stays deterministic even if session
    states change later.
    """
    node = db.get(Node, node_id)
    if node is None:
        raise NotFound("node")
    previous_status = node.status

    if previous_status == FAILED:
        # Idempotent repeat: re-report the already-recorded failure from its
        # immutable event payload (never reconstructed from live sessions).
        existing = (
            db.execute(
                select(event_ledger.Event)
                .where(
                    event_ledger.Event.event_type == NODE_FAILURE_INJECTED,
                    event_ledger.Event.node_id == node.id,
                )
                .order_by(event_ledger.Event.sequence_no.desc())
            )
            .scalars()
            .first()
        )
        if existing is None:  # pragma: no cover - defensive; cannot happen via API
            raise NotFound("node failure event")
        affected_ids = list((existing.payload or {}).get("affected_session_ids", []))
        return node, affected_ids, AppendResult(event=existing, appended=False), False

    if previous_status not in FAILABLE_NODE_STATUSES:
        # Unknown status: change nothing, disconnect nothing, append nothing.
        raise InvalidNodeState(node_id=node.id, status=previous_status)

    # ACTIVE sessions only, deterministic order by session id.
    affected = (
        db.execute(
            select(Session.id)
            .where(Session.node_id == node.id, Session.status == ACTIVE)
            .order_by(Session.id)
        )
        .scalars()
        .all()
    )
    affected_ids = list(affected)

    node.status = FAILED
    if affected_ids:
        sessions = (
            db.execute(select(Session).where(Session.id.in_(affected_ids)))
            .scalars()
            .all()
        )
        for session in sessions:
            session.status = DISCONNECTED
    db.flush()  # NO commit here; append_event() commits flips + event together

    result = event_ledger.append_event(
        db,
        event_type=NODE_FAILURE_INJECTED,
        exam_id=node.exam_id,
        session_id=None,
        candidate_id=None,
        node_id=node.id,
        payload={
            "node_id": node.id,
            "previous_status": previous_status,
            "new_status": FAILED,
            "affected_session_ids": affected_ids,
            "affected_session_count": len(affected_ids),
            "reason": reason,
            "simulation": True,
        },
        client_event_id=None,
        client_timestamp=None,
    )
    return node, affected_ids, result, True
