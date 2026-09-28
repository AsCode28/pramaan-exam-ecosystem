"""Simulation-only API (demo scope).

Endpoints:
- POST /demo/scenario/create -- provision one exam + node + candidates +
  questions for the resilience walkthrough. Deliberately creates NO sessions:
  the demo must call POST /session/start so the normal SESSION_STARTED event
  path is exercised.
- POST /demo/nodes/{node_id}/fail -- intentionally fails an exam node.
- POST /demo/nodes/{node_id}/recover -- recovers a failed node.
- POST /demo/tamper -- corrupts the latest event so audit verification can be
  demonstrated.
- GET /demo/overview/{exam_id} -- read-only operational snapshot (never calls
  Gemini).

This is NOT a production admin API: there is no auth by design, and every
injection is permanently recorded in the Event ledger with simulation=true in
its payload. Nothing here deletes or resets existing data.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import InvalidNodeState, NotFound, SessionApiError
from app.api.http_errors import raise_mapped, refresh_incidents
from app.api.schemas import (
    DemoOverviewResponse,
    DemoResetResponse,
    DemoScenarioCreateResponse,
    DemoTamperRequest,
    DemoTamperResponse,
    FailNodeRequest,
    FailNodeResponse,
    NodeHealthResponse,
    RecoverNodeRequest,
    RecoverNodeResponse,
)
from app.core.database import get_db
from app.core.demo_config import is_demo_mode
from app.db.event import Event
from app.db.exam import Exam
from app.services import (
    demo_reset_service,
    demo_scenario_service,
    failure_service,
    health_service,
    recovery_service,
)

router = APIRouter(prefix="/demo", tags=["demo"])


def _require_demo_mode() -> None:
    """Reject a destructive demo operation unless DEMO_MODE is enabled.

    DEMO_MODE defaults to OFF, so a non-demo deployment can never inject a
    failure, corrupt the ledger, or wipe its database over HTTP. Read-only
    monitoring endpoints are deliberately NOT gated.
    """
    if not is_demo_mode():
        raise HTTPException(
            status_code=403,
            detail=(
                "destructive demo operations are disabled; set DEMO_MODE=true "
                "to enable failure injection, tamper and reset"
            ),
        )


@router.post("/scenario/create", response_model=DemoScenarioCreateResponse)
def create_demo_scenario(db: DbSession = Depends(get_db)):
    """Provision a demo scenario. Creates no sessions and no events."""
    return DemoScenarioCreateResponse(
        **demo_scenario_service.create_demo_scenario(db)
    )


@router.post("/reset", response_model=DemoResetResponse)
def reset_demo(db: DbSession = Depends(get_db)):
    """DESTRUCTIVE. Drop and recreate every table (local SQLite, demo only)."""
    _require_demo_mode()
    try:
        return DemoResetResponse(**demo_reset_service.reset_demo_database(db))
    except demo_reset_service.DemoResetError as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.get("/nodes/{node_id}/health", response_model=NodeHealthResponse)
def get_node_health(node_id: int, db: DbSession = Depends(get_db)):
    """Read-only early-warning signal. Never mutates state, never gated."""
    signal = health_service.node_health(db, node_id)
    if signal is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return NodeHealthResponse(**signal.to_dict())


@router.get("/overview/{exam_id}", response_model=DemoOverviewResponse)
def get_demo_overview(exam_id: int, db: DbSession = Depends(get_db)):
    """Read-only operational snapshot of one exam. Never calls Gemini."""
    overview = demo_scenario_service.build_overview(db, exam_id)
    if overview is None:
        raise HTTPException(status_code=404, detail=f"Exam {exam_id} not found")
    return DemoOverviewResponse(**overview)


@router.post("/nodes/{node_id}/fail", response_model=FailNodeResponse)
def fail_node(node_id: int, body: FailNodeRequest, db: DbSession = Depends(get_db)):
    _require_demo_mode()
    try:
        node, affected_ids, result, newly_failed = failure_service.fail_node(
            db, node_id=node_id, reason=body.reason
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidNodeState as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SessionApiError as exc:
        raise_mapped(exc)

    # Keep incident state current without a manual evaluation call.
    refresh_incidents(db, exam_id=node.exam_id)

    return FailNodeResponse(
        node_id=node.id,
        previous_status=(
            result.event.payload.get("previous_status", node.status)
            if isinstance(result.event.payload, dict)
            else node.status
        ),
        new_status=node.status,
        affected_session_ids=affected_ids,
        affected_count=len(affected_ids),
        event_sequence_no=result.event.sequence_no,
        newly_failed=newly_failed,
    )


@router.post("/nodes/{node_id}/recover", response_model=RecoverNodeResponse)
def recover_node(
    node_id: int, body: RecoverNodeRequest, db: DbSession = Depends(get_db)
):
    """Demo-only recovery: FAILED node -> HEALTHY, sessions -> RECOVERING."""
    _require_demo_mode()
    try:
        node, affected_ids, result, newly_recovered = recovery_service.recover_node(
            db, node_id=node_id, reason=body.reason
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidNodeState as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SessionApiError as exc:
        raise_mapped(exc)

    # Keep incident state current without a manual evaluation call.
    refresh_incidents(db, exam_id=node.exam_id)

    return RecoverNodeResponse(
        node_id=node.id,
        previous_status=(
            result.event.payload.get("previous_status", node.status)
            if isinstance(result.event.payload, dict)
            else node.status
        ),
        new_status=node.status,
        affected_session_ids=affected_ids,
        affected_count=len(affected_ids),
        event_sequence_no=result.event.sequence_no,
        newly_recovered=newly_recovered,
    )


@router.post("/tamper", response_model=DemoTamperResponse)
def tamper_latest_event(body: DemoTamperRequest, db: DbSession = Depends(get_db)):
    """Demo-only simulation: tampered mutation of the latest event in the ledger.

    Strictly simulation-scoped: only target="latest" is accepted, resolving
    directly to the highest Event.sequence_no. Arbitrary sequence numbers cannot
    be supplied. Field must be "payload", "hash", or "previous_hash".
    """
    _require_demo_mode()
    if body.target != "latest":
        raise HTTPException(
            status_code=400,
            detail="Demo tamper only supports target='latest'",
        )

    valid_fields = ("payload", "hash", "previous_hash")
    if body.field not in valid_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid field '{body.field}'. Must be one of {valid_fields}",
        )

    latest_event = db.execute(
        select(Event).order_by(desc(Event.sequence_no)).limit(1)
    ).scalar_one_or_none()

    if latest_event is None:
        raise HTTPException(
            status_code=404,
            detail="Cannot tamper empty ledger: no events found",
        )

    if body.field == "payload":
        current = latest_event.payload if isinstance(latest_event.payload, dict) else {}
        new_payload = dict(current)
        new_payload["__demo_tampered__"] = True
        latest_event.payload = new_payload
        detail = f"Payload modified for sequence {latest_event.sequence_no}"
    elif body.field == "hash":
        orig = latest_event.hash or ""
        flip = "1" if orig.endswith("0") else "0"
        latest_event.hash = (orig[:-1] + flip) if orig else "0" * 64
        detail = f"Hash modified for sequence {latest_event.sequence_no}"
    elif body.field == "previous_hash":
        orig = latest_event.previous_hash or ""
        flip = "1" if orig.endswith("0") else "0"
        latest_event.previous_hash = (orig[:-1] + flip) if orig else "0" * 64
        detail = f"previous_hash modified for sequence {latest_event.sequence_no}"

    db.commit()
    db.refresh(latest_event)

    return DemoTamperResponse(
        tampered_sequence_no=latest_event.sequence_no,
        field=body.field,
        detail=detail,
    )
