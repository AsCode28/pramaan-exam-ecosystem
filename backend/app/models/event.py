from datetime import datetime

from pydantic import BaseModel


class Event(BaseModel):
    id: int
    session_id: int | None
    candidate_id: int | None
    node_id: str | None
    event_type: str
    payload: dict
    client_event_id: str | None
    client_timestamp: datetime | None
    server_timestamp: datetime
    previous_hash: str | None
    hash: str