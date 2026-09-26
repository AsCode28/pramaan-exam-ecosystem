from datetime import datetime

from pydantic import BaseModel


class Incident(BaseModel):
    id: int
    exam_id: int
    severity: str
    status: str
    root_cause_summary: str | None = None
    created_from_event_id: int | None = None
    start_time: datetime
    resolved_time: datetime | None = None