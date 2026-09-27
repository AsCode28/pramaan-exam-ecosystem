"""ORM models for exams, nodes, and questions.

Persistence layer only (SQLAlchemy). These are intentionally separate from
the Pydantic validation models in ``app.models`` -- the two packages never
import from each other.
"""

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.event import Event
    from app.db.incident import Incident
    from app.db.session import Response, Session


class NodeStatus(str, enum.Enum):
    """Health states of an exam node (stored as plain strings in SQLite)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class Exam(Base):
    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    questions: Mapped[list["Question"]] = relationship(back_populates="exam")
    nodes: Mapped[list["Node"]] = relationship(back_populates="exam")
    sessions: Mapped[list["Session"]] = relationship(back_populates="exam")
    incidents: Mapped[list["Incident"]] = relationship(back_populates="exam")
    events: Mapped[list["Event"]] = relationship(back_populates="exam")


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=NodeStatus.HEALTHY.value
    )

    exam: Mapped["Exam"] = relationship(back_populates="nodes")
    sessions: Mapped[list["Session"]] = relationship(back_populates="node")
    events: Mapped[list["Event"]] = relationship(back_populates="node")


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    marks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    exam: Mapped["Exam"] = relationship(back_populates="questions")
    responses: Mapped[list["Response"]] = relationship(back_populates="question")
