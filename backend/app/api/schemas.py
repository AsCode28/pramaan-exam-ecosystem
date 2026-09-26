"""Pydantic request/response schemas for the session API.

These describe the HTTP contract only. They are intentionally separate from
both the SQLAlchemy ORM models (``app.db``) and the legacy validation
sketches in ``app.models`` (left untouched).

Note on ``Response.last_event_id``: it is an integer projection pointer
storing ``Event.sequence_no`` (the events table has no ``id`` column).
The projection guard is ``Response.last_event_id < Event.sequence_no``.
"""

from datetime import datetime

from pydantic import BaseModel


class StartSessionRequest(BaseModel):
    exam_id: int
    candidate_id: int
    node_id: int


class StartSessionResponse(BaseModel):
    session_id: int
    exam_id: int
    candidate_id: int
    node_id: int
    status: str
    started_at: datetime
    last_activity: datetime | None
    event_sequence_no: int


class AnswerRequest(BaseModel):
    question_id: int
    answer: str | None
    client_event_id: str
    client_timestamp: datetime | None = None


class AnswerResponse(BaseModel):
    session_id: int
    question_id: int
    sequence_no: int
    last_event_id: int
    appended: bool
    current_answer: str | None


class HeartbeatRequest(BaseModel):
    client_event_id: str | None = None


class HeartbeatResponse(BaseModel):
    session_id: int
    last_activity: datetime
    sequence_no: int
    appended: bool


class ResponseState(BaseModel):
    question_id: int
    current_answer: str | None
    last_event_id: int | None
    updated_at: datetime


class SessionStateResponse(BaseModel):
    session_id: int
    status: str
    exam_id: int
    candidate_id: int
    node_id: int
    node_status: str
    started_at: datetime
    submitted_at: datetime | None
    last_activity: datetime | None
    responses: list[ResponseState]
    acknowledged_client_event_ids: list[str]
