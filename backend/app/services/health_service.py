"""Deterministic early-warning health signal derived from heartbeat evidence.

Read-oriented: it NEVER mutates the database. A GET against this service
inspects the authoritative ``HEARTBEAT`` events already in the ledger and
reports whether a node is healthy, becoming stale (early warning), or already
FAILED. Nothing is scheduled and no synthetic event is ever created to make
the demo look predictive.

Freshness is always derived from the authoritative ``Event.server_timestamp``.
``client_timestamp`` is untrusted diagnostic metadata and is never used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession

from app.core.demo_config import heartbeat_stale_seconds
from app.db.event import Event
from app.db.exam import Node
from app.db.session import Session

HEARTBEAT = "HEARTBEAT"

HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_FAILED = "FAILED"


@dataclass(frozen=True)
class NodeHealthSignal:
    """Deterministic early-warning signal for one exam node."""

    node_id: int
    health_state: str
    last_heartbeat_server_timestamp: datetime | None
    heartbeat_age_seconds: float | None
    threshold_seconds: int
    reason: str
    early_warning: bool

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "health_state": self.health_state,
            "last_heartbeat_server_timestamp": self.last_heartbeat_server_timestamp,
            "heartbeat_age_seconds": self.heartbeat_age_seconds,
            "threshold_seconds": self.threshold_seconds,
            "reason": self.reason,
            "early_warning": self.early_warning,
        }


def _last_heartbeat(db: DbSession, node_id: int) -> Event | None:
    """Most recent HEARTBEAT for the node or any of its sessions.

    Ordered by ``server_timestamp`` (descending) with ``sequence_no`` as the
    authoritative tie-breaker, never by ``client_timestamp``.
    """
    session_ids = [
        s.id for s in db.scalars(select(Session).where(Session.node_id == node_id)).all()
    ]
    stmt = select(Event).where(Event.event_type == HEARTBEAT)
    if session_ids:
        stmt = stmt.where(
            (Event.node_id == node_id) | (Event.session_id.in_(session_ids))
        )
    return db.scalars(
        stmt.order_by(desc(Event.server_timestamp), desc(Event.sequence_no)).limit(1)
    ).first()


def node_health(db: DbSession, node_id: int) -> NodeHealthSignal | None:
    """Compute the early-warning signal for one node. None if the node is gone."""
    node = db.get(Node, node_id)
    if node is None:
        return None

    threshold = heartbeat_stale_seconds()

    # An already-failed node is FAILED, never merely "stale".
    if node.status == "FAILED":
        return NodeHealthSignal(
            node_id=node.id,
            health_state=HEALTH_FAILED,
            last_heartbeat_server_timestamp=None,
            heartbeat_age_seconds=None,
            threshold_seconds=threshold,
            reason=f"node status is FAILED (threshold {threshold}s)",
            early_warning=False,
        )

    heartbeat = _last_heartbeat(db, node_id)
    if heartbeat is None:
        # No heartbeat evidence at all: nothing has proven this node is stale,
        # and no synthetic event is created to imply otherwise.
        return NodeHealthSignal(
            node_id=node.id,
            health_state=HEALTH_HEALTHY,
            last_heartbeat_server_timestamp=None,
            heartbeat_age_seconds=None,
            threshold_seconds=threshold,
            reason=(
                "no heartbeat recorded yet; cannot compute staleness, "
                "reporting HEALTHY without inventing evidence"
            ),
            early_warning=False,
        )

    last_ts = heartbeat.server_timestamp
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    age = max(0.0, (now - last_ts).total_seconds())

    if age > threshold:
        return NodeHealthSignal(
            node_id=node.id,
            health_state=HEALTH_DEGRADED,
            last_heartbeat_server_timestamp=last_ts,
            heartbeat_age_seconds=round(age, 3),
            threshold_seconds=threshold,
            reason=(
                f"last heartbeat is {age:.1f}s old, over the {threshold}s "
                "stale threshold (early warning, not yet failed)"
            ),
            early_warning=True,
        )

    return NodeHealthSignal(
        node_id=node.id,
        health_state=HEALTH_HEALTHY,
        last_heartbeat_server_timestamp=last_ts,
        heartbeat_age_seconds=round(age, 3),
        threshold_seconds=threshold,
        reason=(
            f"last heartbeat is {age:.1f}s old, within the {threshold}s threshold"
        ),
        early_warning=False,
    )
