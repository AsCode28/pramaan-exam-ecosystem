from pydantic import BaseModel


class Question(BaseModel):
    id: int
    exam_id: int
    text: str
    marks: int