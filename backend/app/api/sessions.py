"""Thin HTTP layer for sessions (no business logic here)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from app.api.errors import SessionApiError
from app.api.http_errors import raise_mapped, refresh_incidents
from app.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    BufferedAnswerEvent,
    HeartbeatRequest,
    HeartbeatResponse,
    ReconcileRequest,
    ReconcileResponse,
    ResponseState,
    SessionStateResponse,
    StartSessionRequest,
    StartSessionResponse,
)
from app.core.database import get_db
from app.services import recovery_service, session_service

router = APIRouter(prefix="/session", tags=["session"])


@router.post("/start", response_model=StartSessionResponse)
def start_session(body: StartSessionRequest, db: DbSession = Depends(get_db)):
    try:
        session, result = session_service.start_session(
            db,
            exam_id=body.exam_id,
            candidate_id=body.candidate_id,
            node_id=body.node_id,
        )
    except SessionApiError as exc:
        raise_mapped(exc)
    return StartSessionResponse(
        session_id=session.id,
        exam_id=session.exam_id,
        candidate_id=session.candidate_id,
        node_id=session.node_id,
        status=session.status,
        started_at=session.started_at,
        last_activity=session.last_activity,
        event_sequence_no=result.event.sequence_no,
    )


@router.post("/{session_id}/answer", response_model=AnswerResponse)
def save_answer(session_id: int, body: AnswerRequest, db: DbSession = Depends(get_db)):
    try:
        response, result = session_service.save_answer(
            db,
            session_id=session_id,
            question_id=body.question_id,
            answer=body.answer,
            client_event_id=body.client_event_id,
            client_timestamp=body.client_timestamp,
        )
    except SessionApiError as exc:
        raise_mapped(exc)
    except Exception as exc:
        if getattr(exc, "projection_failed", False):
            raise HTTPException(
                status_code=500, detail="event stored; projection_failed"
            )
        raise
    return AnswerResponse(
        session_id=session_id,
        question_id=response.question_id,
        sequence_no=result.event.sequence_no,
        last_event_id=response.last_event_id,
        appended=result.appended,
        current_answer=response.current_answer,
    )


@router.post("/{session_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(session_id: int, body: HeartbeatRequest, db: DbSession = Depends(get_db)):
    try:
        session, result = session_service.record_heartbeat(
            db, session_id=session_id, client_event_id=body.client_event_id
        )
    except SessionApiError as exc:
        raise_mapped(exc)
    except Exception as exc:
        if getattr(exc, "projection_failed", False):
            raise HTTPException(
                status_code=500, detail="event stored; projection_failed"
            )
        raise
    return HeartbeatResponse(
        session_id=session.id,
        last_activity=session.last_activity,
        sequence_no=result.event.sequence_no,
        appended=result.appended,
    )


@router.post("/{session_id}/reconcile", response_model=ReconcileResponse)
def reconcile(session_id: int, body: ReconcileRequest, db: DbSession = Depends(get_db)):
    """Accept client-buffered ANSWER_SAVED events for a RECOVERING session."""
    try:
        report = recovery_service.reconcile_session(
            db,
            session_id=session_id,
            items=[item.model_dump() for item in body.events],
        )
    except SessionApiError as exc:
        raise_mapped(exc)
    except Exception as exc:
        if getattr(exc, "projection_failed", False):
            raise HTTPException(
                status_code=500, detail="event stored; projection_failed"
            )
        raise

    # A completed reconciliation can resolve incidents; keep them current
    # without requiring a separate manual evaluation call.
    if report["complete"]:
        refresh_incidents(db, exam_id=report["session"].exam_id)

    return ReconcileResponse(
        session_id=session_id,
        status=report["status"],
        submitted_client_event_ids=report["submitted"],
        acknowledged_client_event_ids=report["acknowledged"],
        newly_reconciled_client_event_ids=report["newly"],
        already_acknowledged_client_event_ids=report["already"],
        missing_client_event_ids=report["missing"],
        mismatched_client_event_ids=report["mismatched"],
        rejected_client_event_ids=report["rejected"],
        rejected_reasons=report["reasons"],
        reconciliation_complete=report["complete"],
        recovered_event_sequence_no=report["recovered_event_sequence_no"],
    )


@router.get("/{session_id}/state", response_model=SessionStateResponse)
def session_state(session_id: int, db: DbSession = Depends(get_db)):
    try:
        state = session_service.get_session_state(db, session_id=session_id)
    except SessionApiError as exc:
        raise_mapped(exc)
    session = state["session"]
    return SessionStateResponse(
        session_id=session.id,
        status=session.status,
        exam_id=session.exam_id,
        candidate_id=session.candidate_id,
        node_id=session.node_id,
        node_status=state["node_status"],
        started_at=session.started_at,
        submitted_at=session.submitted_at,
        last_activity=session.last_activity,
        responses=[
            ResponseState(
                question_id=r.question_id,
                current_answer=r.current_answer,
                last_event_id=r.last_event_id,
                updated_at=r.updated_at,
            )
            for r in state["responses"]
        ],
        acknowledged_client_event_ids=state["acknowledged_client_event_ids"],
    )
