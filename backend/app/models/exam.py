from datetime import datetime

from pydantic import BaseModel


class Exam(BaseModel):
    id: int
    title: str
    status: str
    start_time: datetime
    end_time: datetime