"""Append-only Event ledger service.

Single operation: :func:`append_event`. The ledger is the historical source
of truth, so this module exposes no update/delete for events.

Per append (one database transaction)::

    idempotency check -> previous-hash lookup -> insert + flush
    (SQLite AUTOINCREMENT assigns sequence_no; never MAX()+1)
    -> canonicalize -> SHA-256 -> store hash -> commit

Idempotency: candidate-originated events carrying both ``session_id`` and
``client_event_id`` map to the existing ``UNIQUE(session_id,
client_event_id)`` constraint. A retry returns the already-stored event
with ``appended=False`` and never creates a second row. Genuine database
errors are never swallowed.

Concurrency
-----------
``append_event`` is the one place that reads the tip of the chain and then
appends to it, so the whole read-tip -> insert -> canonicalize -> hash -> commit
section is serialized by a module-level :data:`_APPEND_LOCK`. Without it, two
concurrent writers can both read the same previous hash, producing two events
that claim the same predecessor and silently breaking the chain.

This is **single-process** protection. A ``threading.Lock`` is held in one
interpreter's memory, so it correctly serialises the threaded FastAPI worker
used by the screening prototype, but it does nothing across multiple processes
or multiple machines. Multi-process or multi-replica deployments would need a
database-level guarantee (e.g. a row lock, a serialising transaction, or a
unique constraint on the predecessor hash) rather than an in-process lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.hashing import (
    canonical_event_bytes,
    compute_hash,
    genesis_hash,
)
from app.db.event import Event

# Serialises the append critical section within this process only. See the
# module docstring for why this is not a distributed lock.
_APPEND_LOCK = threading.Lock()


@dataclass(frozen=True)
class AppendResult:
    """Outcome of :func:`append_event`."""

    event: Event
    appended: bool  # False when an idempotent retry returned existing row


def _utcnow_naive() -> datetime:
    """Backend-authoritative timestamp (SQLite stores naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def append_event(
    db: Session,
    *,
    event_type: str,
    exam_id: int | None = None,
    session_id: int | None = None,
    candidate_id: int | None = None,
    node_id: int | None = None,
    payload: Any = None,
    client_event_id: str | None = None,
    client_timestamp: datetime | None = None,
    # NOTE: no server_timestamp parameter. The caller must NOT provide one;
    # the service generates it internally as the authoritative timestamp.
    # client_timestamp stays accepted as untrusted diagnostic metadata only.
) -> AppendResult:
    """Append one event to the ledger atomically.

    Returns ``AppendResult(event, appended=True)`` for a new row, or
    ``AppendResult(existing, appended=False)`` for an idempotent retry.

    The whole read-tip -> insert -> hash -> commit section runs under
    :data:`_APPEND_LOCK` (single-process only; see the module docstring).
    """
    with _APPEND_LOCK:
        return _append_event_locked(
            db,
            event_type=event_type,
            exam_id=exam_id,
            session_id=session_id,
            candidate_id=candidate_id,
            node_id=node_id,
            payload=payload,
            client_event_id=client_event_id,
            client_timestamp=client_timestamp,
        )


def _append_event_locked(
    db: Session,
    *,
    event_type: str,
    exam_id: int | None = None,
    session_id: int | None = None,
    candidate_id: int | None = None,
    node_id: int | None = None,
    payload: Any = None,
    client_event_id: str | None = None,
    client_timestamp: datetime | None = None,
) -> AppendResult:
    """Append one event. Caller must already hold :data:`_APPEND_LOCK`.

    NOTE: no server_timestamp parameter. The caller must NOT provide one; the
    service generates it internally as the authoritative timestamp.
    client_timestamp stays accepted as untrusted diagnostic metadata only.
    """
    try:
        # 1. Idempotency pre-check (same transaction).
        if session_id is not None and client_event_id is not None:
            existing = db.execute(
                select(Event).where(
                    Event.session_id == session_id,
                    Event.client_event_id == client_event_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                db.rollback()
                return AppendResult(event=existing, appended=False)

        # 2. Previous hash: highest sequence_no ledger-wide, else genesis.
        latest_hash = db.execute(
            select(Event.hash).order_by(desc(Event.sequence_no)).limit(1)
        ).scalar_one_or_none()
        previous_hash = latest_hash if latest_hash is not None else genesis_hash(exam_id)

        # 3. Insert; flush lets SQLite AUTOINCREMENT assign sequence_no.
        # server_timestamp is backend-generated: authoritative, not caller input.
        event = Event(
            exam_id=exam_id,
            session_id=session_id,
            candidate_id=candidate_id,
            node_id=node_id,
            event_type=event_type,
            payload=payload,
            client_event_id=client_event_id,
            client_timestamp=client_timestamp,
            server_timestamp=_utcnow_naive(),
            previous_hash=previous_hash,
            hash=None,
        )
        db.add(event)
        db.flush()  # sequence_no assigned here, inside the same transaction

        # 4-5. Canonicalize (includes sequence_no) -> hash -> persist.
        canonical = canonical_event_bytes(
            sequence_no=event.sequence_no,
            exam_id=event.exam_id,
            session_id=event.session_id,
            candidate_id=event.candidate_id,
            node_id=event.node_id,
            event_type=event.event_type,
            payload=event.payload,
            client_event_id=event.client_event_id,
            client_timestamp=event.client_timestamp,
            server_timestamp=event.server_timestamp,
            previous_hash=event.previous_hash,
        )
        event.hash = compute_hash(canonical, previous_hash)
        db.flush()

        # 6. Atomic commit of insert + sequence + hash.
        db.commit()
        db.refresh(event)
        return AppendResult(event=event, appended=True)
    except IntegrityError:
        # Lost race on UNIQUE(session_id, client_event_id): the winner's row
        # is the canonical outcome; return it. Anything else re-raises.
        db.rollback()
        if session_id is not None and client_event_id is not None:
            existing = db.execute(
                select(Event).where(
                    Event.session_id == session_id,
                    Event.client_event_id == client_event_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return AppendResult(event=existing, appended=False)
        raise
    except Exception:
        db.rollback()
        raise
