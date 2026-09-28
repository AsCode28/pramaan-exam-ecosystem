"""Pydantic request/response schemas for the session API.

These describe the HTTP contract only. They are intentionally separate from
both the SQLAlchemy ORM models (``app.db``) and the legacy validation
sketches in ``app.models`` (left untouched).

Note on ``Response.last_event_id``: it is an integer projection pointer
storing ``Event.sequence_no`` (the events table has no ``id`` column).
The projection guard is ``Response.last_event_id < Event.sequence_no``.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness probe. Intentionally minimal."""

    status: str


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


class FailNodeRequest(BaseModel):
    reason: str | None = None


class FailNodeResponse(BaseModel):
    node_id: int
    previous_status: str
    new_status: str
    affected_session_ids: list[int]
    affected_count: int
    event_sequence_no: int
    newly_failed: bool


class RecoverNodeRequest(BaseModel):
    reason: str | None = None


class RecoverNodeResponse(BaseModel):
    node_id: int
    previous_status: str
    new_status: str
    affected_session_ids: list[int]
    affected_count: int
    event_sequence_no: int
    newly_recovered: bool


class BufferedAnswerEvent(BaseModel):
    """One client-buffered answer; ANSWER_SAVED reconciliation only."""

    client_event_id: str
    question_id: int
    answer: str | None = None
    client_timestamp: datetime | None = None


class ReconcileRequest(BaseModel):
    # default_factory (not a shared []) so one request can never mutate the
    # default seen by the next one.
    events: list[BufferedAnswerEvent] = Field(default_factory=list)


class ReconcileResponse(BaseModel):
    session_id: int
    status: str
    submitted_client_event_ids: list[str]
    acknowledged_client_event_ids: list[str]
    newly_reconciled_client_event_ids: list[str]
    already_acknowledged_client_event_ids: list[str]
    missing_client_event_ids: list[str]
    mismatched_client_event_ids: list[str]
    rejected_client_event_ids: list[str]
    rejected_reasons: dict[str, str] = Field(default_factory=dict)
    reconciliation_complete: bool
    recovered_event_sequence_no: int | None = None


class FailNodeResponse(BaseModel):
    node_id: int
    previous_status: str
    new_status: str
    affected_session_ids: list[int]
    affected_count: int
    event_sequence_no: int
    newly_failed: bool


class IncidentResponse(BaseModel):
    """One incident plus the ledger evidence that grounds it.

    ``created_from_event_id`` is an ``Event.sequence_no`` (the events table has
    no ``id`` column); it anchors the incident episode and, for NODE incidents,
    resolves the node through the origin event.
    """

    id: int
    exam_id: int
    severity: str
    status: str
    root_cause_summary: str | None = None
    created_from_event_id: int | None = None
    detected_at: datetime
    resolved_at: datetime | None = None
    affected_session_ids: list[int] = Field(default_factory=list)
    evidence_event_sequence_nos: list[int] = Field(default_factory=list)


class IncidentListResponse(BaseModel):
    incidents: list[IncidentResponse]
    total: int


class IncidentEvaluateRequest(BaseModel):
    exam_id: int


class IncidentEvaluateResponse(BaseModel):
    exam_id: int
    evaluated_at: datetime
    incidents: list[IncidentResponse]



class AuditVerifyResponse(BaseModel):
    valid: bool
    events_checked: int
    first_broken_sequence_no: int | None
    failure_reason: str | None


class DemoTamperRequest(BaseModel):
    target: str = "latest"
    field: str  # "payload" | "hash" | "previous_hash"


class DemoTamperResponse(BaseModel):
    tampered_sequence_no: int
    field: str
    detail: str


class NodeHealthResponse(BaseModel):
    """Read-only early-warning signal derived from heartbeat evidence.

    Never mutates state. Freshness comes from the authoritative
    ``Event.server_timestamp``; ``client_timestamp`` is never used.
    """

    node_id: int
    health_state: str
    last_heartbeat_server_timestamp: datetime | None
    heartbeat_age_seconds: float | None
    threshold_seconds: int
    reason: str
    early_warning: bool


class DemoResetResponse(BaseModel):
    """Explicit, destructive, demo-only reset report."""

    reset: bool
    database_scheme: str
    tables_recreated: list[str]


class EvidenceEventDetail(BaseModel):
    """One grounded evidence event, exactly as stored in the ledger."""

    sequence_no: int
    event_type: str
    exam_id: int | None
    session_id: int | None
    candidate_id: int | None
    node_id: int | None
    payload: dict | None
    server_timestamp: datetime
    previous_hash: str | None
    hash: str | None


class IncidentEvidenceSession(BaseModel):
    id: int
    exam_id: int
    candidate_id: int
    node_id: int
    status: str
    started_at: datetime
    submitted_at: datetime | None
    last_activity: datetime | None


class IncidentEvidenceNode(BaseModel):
    id: int
    exam_id: int
    status: str


class IncidentEvidenceSummary(BaseModel):
    id: int
    exam_id: int
    severity: str
    status: str
    root_cause_summary: str | None
    created_from_event_id: int | None
    detected_at: datetime
    resolved_at: datetime | None


class IncidentEvidenceResponse(BaseModel):
    """Read-only evidence package for one incident."""

    incident: IncidentEvidenceSummary
    affected_session_ids: list[int]
    affected_node_ids: list[int]
    sessions: list[IncidentEvidenceSession]
    nodes: list[IncidentEvidenceNode]
    evidence_events: list[EvidenceEventDetail]
    recovery_facts: dict
    audit_status: AuditVerifyResponse


class AIAnalysisResponse(BaseModel):
    """Explanatory-only grounded analysis. Never mutates ledger state."""

    incident_id: int
    likely_cause: str
    impact_summary: str
    recommended_response: str
    evidence_refs: list[int]
    generated_at: datetime


class DemoScenarioCreateResponse(BaseModel):
    """One demo exam with exactly one node, 3 candidates and 3 questions.

    No sessions are created: the demo must call POST /session/start so the
    normal SESSION_STARTED event path is exercised.
    """

    exam_id: int
    node_id: int
    candidate_ids: list[int]
    question_ids: list[int]


class DemoOverviewExam(BaseModel):
    id: int
    title: str
    status: str
    start_time: datetime
    end_time: datetime


class DemoOverviewNode(BaseModel):
    id: int
    exam_id: int
    status: str
    # Reuses the read-only early-warning health signal.
    health: NodeHealthResponse


class DemoOverviewSession(BaseModel):
    id: int
    candidate_id: int
    node_id: int
    status: str
    last_activity: datetime | None


class DemoOverviewIncident(BaseModel):
    id: int
    severity: str
    status: str
    root_cause_summary: str | None
    created_from_event_id: int | None
    detected_at: datetime
    resolved_at: datetime | None
    affected_session_ids: list[int]
    evidence_event_sequence_nos: list[int]


class DemoOverviewResponse(BaseModel):
    """Read-only operational snapshot. Never calls Gemini."""

    exam: DemoOverviewExam
    nodes: list[DemoOverviewNode]
    sessions: list[DemoOverviewSession]
    incidents: list[DemoOverviewIncident]
    audit_status: AuditVerifyResponse
    latest_event_sequence_no: int | None
