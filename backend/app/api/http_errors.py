"""Shared domain-error -> HTTP status mapping for the routers.

Lives in its own module (not in :mod:`app.api.errors`) so the service layer,
which imports the domain errors, never pulls in FastAPI. Every router uses this
one mapper so a given domain error always yields the same status:

    NotFound             -> 404  resource does not exist
    NodeUnavailable      -> 503  infrastructure down; retry after recovery
    InvalidState         -> 409  conflicting / rejected operation
    ForeignExamQuestion  -> 422  payload validation
    anything else        -> 500  generic, never leaking internals
"""

from fastapi import HTTPException

from app.api.errors import (
    ForeignExamQuestion,
    InvalidState,
    NodeUnavailable,
    NotFound,
    SessionApiError,
)


def raise_mapped(exc: SessionApiError) -> None:
    """Translate a domain error into its HTTP counterpart. Always raises."""
    if isinstance(exc, NotFound):
        raise HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, NodeUnavailable):
        # Infrastructure-level outage: the client should retry after recovery.
        raise HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, InvalidState):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ForeignExamQuestion):
        raise HTTPException(status_code=422, detail=str(exc))
    # Unexpected domain failure: never leak the internal message.
    raise HTTPException(status_code=500, detail="internal error")


def refresh_incidents(db, exam_id: int) -> None:
    """Re-project incidents for one exam after an event-producing operation.

    Keeps incident state current without polluting any GET endpoint with hidden
    writes: the caller invokes this only after a failure / recovery /
    reconciliation has already committed. Evaluation is idempotent, so repeated
    triggers never duplicate an incident, and a projection problem here must
    never undo the operation that already succeeded.
    """
    from app.services import incident_service

    try:
        incident_service.evaluate_incidents(db, exam_id=exam_id)
    except Exception:
        # The primary operation is already committed and authoritative; a
        # projection hiccup must not turn its success into a 500.
        db.rollback()
