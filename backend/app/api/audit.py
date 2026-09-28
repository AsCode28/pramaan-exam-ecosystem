"""Audit verification API endpoint.

POST /audit/verify runs global chain verification over the entire append-only
Event ledger and returns the verification report.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DbSession

from app.api.schemas import AuditVerifyResponse
from app.core.database import get_db
from app.services import audit_service

router = APIRouter(prefix="/audit", tags=["audit"])


@router.post("/verify", response_model=AuditVerifyResponse)
def verify_audit_ledger(db: DbSession = Depends(get_db)):
    """Verify the entire global Event ledger chain."""
    result = audit_service.verify_ledger_chain(db)
    return AuditVerifyResponse(
        valid=result.valid,
        events_checked=result.events_checked,
        first_broken_sequence_no=result.first_broken_sequence_no,
        failure_reason=result.failure_reason,
    )
