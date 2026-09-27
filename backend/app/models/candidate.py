from pydantic import BaseModel


class Candidate(BaseModel):
    id: int
    name: str
    roll_no: str