from datetime import datetime

from pydantic import BaseModel


class Session(BaseModel):
    id: int
    exam_id: int
    candidate_id: int
    node_id: str
    status: str
    started_at: datetime
    last_activity: datetime