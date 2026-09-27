"""Tests for the append-only Event ledger + SHA-256 hash chain.

Each test uses an isolated SQLite file under ``tmp_path``; the production
``pramaan.db`` is never touched.
"""

import hashlib
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.hashing import (
    HEX64_RE,
    canonical_event_bytes,
    genesis_hash,
    verify_event,
)
from app.db import Base, Event
from app.services.event_ledger import append_event

EXAM_ID = 7


@pytest.fixture()
def db(tmp_path):
    """Fresh isolated database session per test."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/test_ledger.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def candidate_append(db, client_event_id, **overrides):
    """Append a candidate-originated event with stable defaults."""
    params = {
        "exam_id": EXAM_ID,
        "session_id": 11,
        "candidate_id": 21,
        "node_id": 31,
        "event_type": "ANSWER_SELECTED",
        "payload": {"question": 1, "answer": "A"},
        "client_event_id": client_event_id,
    }
    params.update(overrides)
    return append_event(db, **params)


def event_kwargs(event):
    """Field snapshot of a stored event for independent re-verification."""
    return {
        "sequence_no": event.sequence_no,
        "exam_id": event.exam_id,
        "session_id": event.session_id,
        "candidate_id": event.candidate_id,
        "node_id": event.node_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "client_event_id": event.client_event_id,
        "client_timestamp": event.client_timestamp,
        "server_timestamp": event.server_timestamp,
        "previous_hash": event.previous_hash,
        "expected_hash": event.hash,
    }


def test_append_event_has_no_server_timestamp_parameter():
    """Server timestamp is backend-generated; callers cannot supply it."""
    assert "server_timestamp" not in inspect.signature(append_event).parameters


def test_first_event_gets_sequence_no_one(db):
    result = candidate_append(db, "evt-1")
    assert result.appended is True
    assert result.event.sequence_no == 1


def test_first_event_previous_hash_is_genesis_digest(db):
    result = candidate_append(db, "evt-1")
    expected = hashlib.sha256(f"PRAMAAN-GENESIS-{EXAM_ID}".encode("utf-8")).hexdigest()
    assert result.event.previous_hash == expected
    assert result.event.previous_hash == genesis_hash(EXAM_ID)
    assert HEX64_RE.match(result.event.previous_hash), "must be a real SHA-256 digest"


def test_second_event_sequence_greater_than_first(db):
    first = candidate_append(db, "evt-1")
    second = candidate_append(db, "evt-2")
    assert second.event.sequence_no > first.event.sequence_no


def test_second_event_links_to_first_hash(db):
    first = candidate_append(db, "evt-1")
    second = candidate_append(db, "evt-2")
    assert second.event.previous_hash == first.event.hash
    assert HEX64_RE.match(second.event.previous_hash)
    assert HEX64_RE.match(first.event.hash)


def test_third_event_links_to_second_hash(db):
    candidate_append(db, "evt-1")
    second = candidate_append(db, "evt-2")
    third = candidate_append(db, "evt-3")
    assert third.event.previous_hash == second.event.hash


def test_first_event_hash_recomputes_independently(db):
    """verify_event reconstructs SHA256(canonical || genesis_hash)."""
    result = candidate_append(db, "evt-1")
    assert verify_event(**event_kwargs(result.event)) is True


def test_chain_hashes_recompute_independently(db):
    first = candidate_append(db, "evt-1")
    second = candidate_append(db, "evt-2")
    third = candidate_append(db, "evt-3")
    for result in (first, second, third):
        assert verify_event(**event_kwargs(result.event)) is True


def test_identical_retry_does_not_duplicate(db):
    first = candidate_append(db, "evt-1")
    retry = candidate_append(db, "evt-1")
    assert retry.appended is False
    assert retry.event.sequence_no == first.event.sequence_no
    count = db.execute(
        select(func.count())
        .select_from(Event)
        .where(Event.session_id == 11, Event.client_event_id == "evt-1")
    ).scalar_one()
    assert count == 1


def test_different_client_event_ids_create_distinct_events(db):
    first = candidate_append(db, "evt-1")
    second = candidate_append(db, "evt-2")
    assert second.appended is True
    assert second.event.sequence_no != first.event.sequence_no
    count = db.execute(select(func.count()).select_from(Event)).scalar_one()
    assert count == 2


def test_tampered_payload_fails_verification(db):
    result = candidate_append(db, "evt-1")
    tampered = event_kwargs(result.event)
    tampered["payload"] = {"question": 1, "answer": "TAMPERED"}
    assert verify_event(**tampered) is False


def test_canonical_serialization_is_deterministic(db):
    """Shuffled payload keys and aware-vs-naive datetimes, same bytes."""
    naive = datetime(2026, 9, 26, 10, 0, 0, 123456)
    aware = naive.replace(tzinfo=timezone.utc)
    common = {
        "sequence_no": 1,
        "exam_id": EXAM_ID,
        "session_id": 11,
        "candidate_id": 21,
        "node_id": 31,
        "event_type": "ANSWER_SELECTED",
        "client_event_id": "evt-1",
        "previous_hash": genesis_hash(EXAM_ID),
    }
    first = canonical_event_bytes(
        payload={"b": 1, "a": [3, 2, {"z": 0, "y": 0}]},
        client_timestamp=naive,
        server_timestamp=naive,
        **common,
    )
    second = canonical_event_bytes(
        payload={"a": [3, 2, {"y": 0, "z": 0}], "b": 1},
        client_timestamp=aware,
        server_timestamp=aware,
        **common,
    )
    assert first == second


def test_order_follows_sequence_not_client_timestamp(db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    early = candidate_append(
        db, "evt-early-client", client_timestamp=now + timedelta(hours=1)
    )
    late = candidate_append(
        db, "evt-late-client", client_timestamp=now - timedelta(hours=1)
    )
    by_sequence = db.execute(
        select(Event.client_event_id).order_by(Event.sequence_no)
    ).scalars().all()
    by_client_ts = db.execute(
        select(Event.client_event_id).order_by(Event.client_timestamp)
    ).scalars().all()
    assert by_sequence == [early.event.client_event_id, late.event.client_event_id]
    assert by_client_ts == [late.event.client_event_id, early.event.client_event_id]
    assert by_sequence != by_client_ts