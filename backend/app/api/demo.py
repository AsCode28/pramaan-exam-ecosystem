"""Simulation-only failure-injection API (demo scope).

POST /demo/nodes/{node_id}/fail intentionally fails an exam node for the
hackathon demo. This is NOT a production admin API: there is no auth by
design, and every injection is permanently recorded in the Event ledger
with simulation=true in its payload.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DbSession

from app.api.errors import InvalidNodeState, NotFound, SessionApiError
from app.api.schemas import (
    FailNodeRequest,
    FailNodeResponse,
    RecoverNodeRequest,
    RecoverNodeResponse,
)
from app.core.database import get_db
from app.services import failure_service, recovery_service

router = APIRouter(prefix="/demo", tags=["demo"])


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
