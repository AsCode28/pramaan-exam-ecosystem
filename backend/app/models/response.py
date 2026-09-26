from datetime import datetime

from pydantic import BaseModel


class Response(BaseModel):
    id: int
    session_id: int
    question_id: int
    answer: str
    version: int
    client_event_id: str
    client_timestamp: datetime
    sync_status: str