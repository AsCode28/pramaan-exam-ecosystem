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
from app.api.schemas import (
    DemoOverviewResponse,
    DemoScenarioCreateResponse,
    DemoTamperRequest,
    DemoTamperResponse,
    FailNodeRequest,
    FailNodeResponse,
    RecoverNodeRequest,
    RecoverNodeResponse,
)
from app.core.database import get_db
from app.db.event import Event
from app.services import demo_scenario_service, failure_service, recovery_service

router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/scenario/create", response_model=DemoScenarioCreateResponse)
def create_demo_scenario(db: DbSession = Depends(get_db)):
    """Provision a demo scenario. Creates no sessions and no events."""
    return DemoScenarioCreateResponse(
        **demo_scenario_service.create_demo_scenario(db)
    )


@router.get("/overview/{exam_id}", response_model=DemoOverviewResponse)
def get_demo_overview(exam_id: int, db: DbSession = Depends(get_db)):
    """Read-only operational snapshot of one exam. Never calls Gemini."""
    overview = demo_scenario_service.build_overview(db, exam_id)
    if overview is None:
        raise HTTPException(status_code=404, detail=f"Exam {exam_id} not found")
    return DemoOverviewResponse(**overview)


@router.post("/nodes/{node_id}/fail", response_model=FailNodeResponse)
def fail_node(node_id: int, body: FailNodeRequest, db: DbSession = Depends(get_db)):
    try:
        node, affected_ids, result, newly_failed = failure_service.fail_node(
            db, node_id=node_id, reason=body.reason
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidNodeState as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SessionApiError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
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
    try:
        node, affected_ids, result, newly_recovered = recovery_service.recover_node(
            db, node_id=node_id, reason=body.reason
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InvalidNodeState as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except SessionApiError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
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
