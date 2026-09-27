"""Hashing primitives for the PRAMAAN append-only Event ledger.

Single home of all hashing logic. Nothing else in the codebase computes
event hashes.

Chain definition
----------------
For event *i* with canonical bytes ``C_i`` and predecessor hash ``H_{i-1}``::

    H_i = SHA256hex(C_i || H_{i-1})

where ``||`` is byte concatenation and ``H_{i-1}`` is the UTF-8 encoding of
the predecessor's 64-character hex digest.

Genesis
-------
The ledger has no predecessor for the first event, so a deterministic
genesis value is derived from the exam context::

    genesis_material = "PRAMAAN-GENESIS-{exam_id}"
                     | "PRAMAAN-GENESIS-GLOBAL"   (when exam_id is None)

    genesis_hash     = SHA256hex(genesis_material)

The first event stores ``genesis_hash`` (a real 64-char hex digest) as its
``previous_hash``. The human-readable ``PRAMAAN-GENESIS-{exam_id}`` string
is only the pre-image material and is NEVER stored in the database, so
every populated ``previous_hash`` in the table is an actual SHA-256 digest.
The chain is global: the predecessor is always the highest ``sequence_no``
ledger-wide, regardless of exam.

Canonical serialization
-----------------------
``canonical_event_bytes`` covers exactly these fields (``hash`` excluded)::

    sequence_no, exam_id, session_id, candidate_id, node_id, event_type,
    payload, client_event_id, client_timestamp, server_timestamp,
    previous_hash

Timestamp roles: ``client_timestamp`` is untrusted client-supplied
diagnostic metadata; ``server_timestamp`` is the backend-generated
authoritative timestamp. Both are bound into the hash, but canonical event
ordering always comes from ``sequence_no``.

Determinism rules: one plain dict, ``json.dumps`` with ``sort_keys=True``
and ``separators=(",", ":")`` (no whitespace ambiguity), UTF-8 encoding,
no ``str()``/``repr()`` of Python objects. Datetimes are normalised to
naive UTC and rendered with ``isoformat(timespec="microseconds")``.
``None`` renders as JSON ``null``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

# Field order is documentation only; json.dumps(sort_keys=True) enforces it.
CANONICAL_FIELDS = (
    "sequence_no",
    "exam_id",
    "session_id",
    "candidate_id",
    "node_id",
    "event_type",
    "payload",
    "client_event_id",
    "client_timestamp",
    "server_timestamp",
    "previous_hash",
)

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def genesis_material(exam_id: int | None) -> str:
    """Human-readable pre-image for the genesis hash (never stored)."""
    if exam_id is None:
        return "PRAMAAN-GENESIS-GLOBAL"
    return f"PRAMAAN-GENESIS-{exam_id}"


def genesis_hash(exam_id: int | None) -> str:
    """Deterministic genesis digest used as ``previous_hash`` of event #1."""
    return hashlib.sha256(genesis_material(exam_id).encode("utf-8")).hexdigest()


def normalise_datetime(value: datetime | None) -> str | None:
    """Render a datetime as stable naive-UTC ISO-8601 with microseconds."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec="microseconds")


def canonical_event_bytes(
    *,
    sequence_no: int,
    exam_id: int | None,
    session_id: int | None,
    candidate_id: int | None,
    node_id: int | None,
    event_type: str,
    payload: Any,
    client_event_id: str | None,
    client_timestamp: datetime | None,
    server_timestamp: datetime | None,
    previous_hash: str | None,
) -> bytes:
    """Deterministic canonical bytes for one event (``hash`` excluded)."""
    canonical = {
        "sequence_no": sequence_no,
        "exam_id": exam_id,
        "session_id": session_id,
        "candidate_id": candidate_id,
        "node_id": node_id,
        "event_type": event_type,
        "payload": payload,
        "client_event_id": client_event_id,
        "client_timestamp": normalise_datetime(client_timestamp),
        "server_timestamp": normalise_datetime(server_timestamp),
        "previous_hash": previous_hash,
    }
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def compute_hash(canonical: bytes, previous_hash: str) -> str:
    """H_i = SHA256hex(canonical_bytes_i || previous_hash)."""
    return hashlib.sha256(canonical + previous_hash.encode("utf-8")).hexdigest()


def verify_event(
    *,
    sequence_no: int,
    exam_id: int | None,
    session_id: int | None,
    candidate_id: int | None,
    node_id: int | None,
    event_type: str,
    payload: Any,
    client_event_id: str | None,
    client_timestamp: datetime | None,
    server_timestamp: datetime | None,
    previous_hash: str,
    expected_hash: str,
) -> bool:
    """Recompute independently; True iff the stored hash matches.

    The future audit task reuses this to reconstruct
    ``H_i = SHA256(CanonicalEvent_i || H_{i-1})`` and find the first break.
    """
    canonical = canonical_event_bytes(
        sequence_no=sequence_no,
        exam_id=exam_id,
        session_id=session_id,
        candidate_id=candidate_id,
        node_id=node_id,
        event_type=event_type,
        payload=payload,
        client_event_id=client_event_id,
        client_timestamp=client_timestamp,
        server_timestamp=server_timestamp,
        previous_hash=previous_hash,
    )
    return compute_hash(canonical, previous_hash) == expected_hash
