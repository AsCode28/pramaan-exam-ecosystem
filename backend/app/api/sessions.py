"""Thin HTTP layer for sessions (no business logic here)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from app.api.errors import (
    ForeignExamQuestion,
    InvalidState,
    NodeUnavailable,
    NotFound,
    SessionApiError,
)
from app.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    ResponseState,
    SessionStateResponse,
    StartSessionRequest,
    StartSessionResponse,
)
from app.core.database import get_db
from app.services import session_service

router = APIRouter(prefix="/session", tags=["session"])


def _raise_mapped(exc: SessionApiError) -> None:
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, NodeUnavailable):
        # Infrastructure-level outage: the client should retry after recovery.
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, InvalidState):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ForeignExamQuestion):
        raise HTTPException(status_code=422, detail=str(exc))
    raise HTTPException(status_code=500, detail="internal error")


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
        _raise_mapped(exc)
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
        _raise_mapped(exc)
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
        _raise_mapped(exc)
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


@router.get("/{session_id}/state", response_model=SessionStateResponse)
def session_state(session_id: int, db: DbSession = Depends(get_db)):
    try:
        state = session_service.get_session_state(db, session_id=session_id)
    except SessionApiError as exc:
        _raise_mapped(exc)
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
