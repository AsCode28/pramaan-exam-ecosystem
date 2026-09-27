"""Audit verification service for the append-only Event ledger.

Verifies the complete global Event ledger chain:
- Ordering strictly in sequence_no order.
- Sequence starts at sequence_no == 1.
- First event links to the deterministic genesis hash derived from its exam_id:
    genesis_hash(first_event.exam_id).
- Every subsequent event has sequence_no == previous.sequence_no + 1.
- Every subsequent event has previous_hash == previous.hash.
- Recomputed hash matches stored hash independently for every event.
- Stops and reports at the FIRST broken event.
- events_checked counts all events inspected, INCLUDING the broken event.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.hashing import (
    canonical_event_bytes,
    compute_hash,
    genesis_hash,
)
from app.db.event import Event


@dataclass(frozen=True)
class AuditVerificationResult:
    valid: bool
    events_checked: int
    first_broken_sequence_no: int | None
    failure_reason: str | None


def verify_ledger_chain(db: Session) -> AuditVerificationResult:
    """Verify the entire global Event ledger chain.

    Returns an AuditVerificationResult. Never modifies Event rows.
    """
    events = (
        db.execute(select(Event).order_by(Event.sequence_no.asc()))
        .scalars()
        .all()
    )

    if not events:
        return AuditVerificationResult(
            valid=True,
            events_checked=0,
            first_broken_sequence_no=None,
            failure_reason=None,
        )

    # 1. Verify the first event
    first_event = events[0]
    events_checked = 1

    if first_event.sequence_no != 1:
        return AuditVerificationResult(
            valid=False,
            events_checked=events_checked,
            first_broken_sequence_no=first_event.sequence_no,
            failure_reason=(
                f"Broken sequence relationship at sequence {first_event.sequence_no}: "
                f"first event must have sequence_no == 1, found {first_event.sequence_no}"
            ),
        )

    expected_genesis = genesis_hash(first_event.exam_id)
    if first_event.previous_hash != expected_genesis:
        return AuditVerificationResult(
            valid=False,
            events_checked=events_checked,
            first_broken_sequence_no=first_event.sequence_no,
            failure_reason=(
                f"Genesis mismatch at sequence {first_event.sequence_no}: "
                f"expected {expected_genesis}, found {first_event.previous_hash}"
            ),
        )

    canonical_0 = canonical_event_bytes(
        sequence_no=first_event.sequence_no,
        exam_id=first_event.exam_id,
        session_id=first_event.session_id,
        candidate_id=first_event.candidate_id,
        node_id=first_event.node_id,
        event_type=first_event.event_type,
        payload=first_event.payload,
        client_event_id=first_event.client_event_id,
        client_timestamp=first_event.client_timestamp,
        server_timestamp=first_event.server_timestamp,
        previous_hash=first_event.previous_hash,
    )
    recomputed_0 = compute_hash(canonical_0, first_event.previous_hash)
    if recomputed_0 != first_event.hash:
        return AuditVerificationResult(
            valid=False,
            events_checked=events_checked,
            first_broken_sequence_no=first_event.sequence_no,
            failure_reason=(
                f"Hash mismatch at sequence {first_event.sequence_no}: "
                f"expected {recomputed_0}, found {first_event.hash}"
            ),
        )

    # 2. Verify all subsequent events
    for i in range(1, len(events)):
        previous = events[i - 1]
        current = events[i]
        events_checked = i + 1

        if current.sequence_no != previous.sequence_no + 1:
            return AuditVerificationResult(
                valid=False,
                events_checked=events_checked,
                first_broken_sequence_no=current.sequence_no,
                failure_reason=(
                    f"Broken sequence relationship at sequence {current.sequence_no}: "
                    f"expected {previous.sequence_no + 1}, found {current.sequence_no}"
                ),
            )

        if current.previous_hash != previous.hash:
            return AuditVerificationResult(
                valid=False,
                events_checked=events_checked,
                first_broken_sequence_no=current.sequence_no,
                failure_reason=(
                    f"Broken previous_hash link at sequence {current.sequence_no}: "
                    f"expected {previous.hash}, found {current.previous_hash}"
                ),
            )

        canonical = canonical_event_bytes(
            sequence_no=current.sequence_no,
            exam_id=current.exam_id,
            session_id=current.session_id,
            candidate_id=current.candidate_id,
            node_id=current.node_id,
            event_type=current.event_type,
            payload=current.payload,
            client_event_id=current.client_event_id,
            client_timestamp=current.client_timestamp,
            server_timestamp=current.server_timestamp,
            previous_hash=current.previous_hash,
        )
        recomputed = compute_hash(canonical, current.previous_hash)
        if recomputed != current.hash:
            return AuditVerificationResult(
                valid=False,
                events_checked=events_checked,
                first_broken_sequence_no=current.sequence_no,
                failure_reason=(
                    f"Hash mismatch at sequence {current.sequence_no}: "
                    f"expected {recomputed}, found {current.hash}"
                ),
            )

    return AuditVerificationResult(
        valid=True,
        events_checked=len(events),
        first_broken_sequence_no=None,
        failure_reason=None,
    )
