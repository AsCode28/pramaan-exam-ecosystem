"""Incident API router (Task 6).

Read/projection surface of the incident engine. Evaluation is explicit and
synchronous -- nothing here schedules work in the background and nothing here
writes to the Event ledger.

Endpoints:
- POST /demo/incidents/evaluate -- evaluate one exam and return its incidents
  (demo scope, like the failure/recovery injection endpoints).
- GET /incidents?exam_id=... -- list the incidents of an exam, optionally
  filtered by status.
- GET /incident/{id} -- one incident with its grounded evidence sequence
  numbers.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.errors import NotFound
from app.api.schemas import (
    IncidentEvaluateRequest,
    IncidentEvaluateResponse,
    IncidentListResponse,
    IncidentResponse,
)
from app.core.database import get_db
from app.db.incident import Incident
from app.services import incident_service

router = APIRouter(tags=["incidents"])


def _as_response(db: DbSession, incident: Incident) -> IncidentResponse:
    return IncidentResponse(**incident_service.build_incident_response(db, incident))


@router.post("/demo/incidents/evaluate", response_model=IncidentEvaluateResponse)
def evaluate_incidents(
    body: IncidentEvaluateRequest, db: DbSession = Depends(get_db)
):
    """Detect, escalate and resolve incidents for one exam."""
    try:
        incidents = incident_service.evaluate_incidents(db, exam_id=body.exam_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return IncidentEvaluateResponse(
        exam_id=body.exam_id,
        evaluated_at=incident_service.utcnow(),
        incidents=[_as_response(db, incident) for incident in incidents],
    )


@router.get("/incidents", response_model=IncidentListResponse)
def list_incidents(
    exam_id: int = Query(..., description="ID of the exam"),
    status: str | None = Query(
        None, description="Optional status filter: ACTIVE or RESOLVED"
    ),
    db: DbSession = Depends(get_db),
):
    stmt = select(Incident).where(Incident.exam_id == exam_id)
    if status is not None:
        stmt = stmt.where(Incident.status == status)
    incidents = db.scalars(stmt.order_by(Incident.id.asc())).all()
    return IncidentListResponse(
        incidents=[_as_response(db, incident) for incident in incidents],
        total=len(incidents),
    )


@router.get("/incident/{id}", response_model=IncidentResponse)
def get_incident(id: int, db: DbSession = Depends(get_db)):
    incident = db.get(Incident, id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"Incident {id} not found")
    return _as_response(db, incident)
