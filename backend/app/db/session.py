"""ORM models for sessions and responses.

Persistence layer only (SQLAlchemy). Separate from the Pydantic
validation models in ``app.models``.

Notes:
- ``Session.node_id`` is a foreign key to ``nodes.id``; it is NOT a
  free-form field (unlike the Pydantic ``Session`` sketch).
- ``Response`` is a projection / current-state table populated from the
  Event ledger. ``last_event_id`` is therefore nullable at the schema
  level; the later response service will populate it.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.event import Event
    from app.db.exam import Exam, Node, Question
    from app.db.candidate import Candidate
    from app.db.incident import IncidentSession


def _utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone-aware DATETIME)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), nullable=False)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id"), nullable=False
    )
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    exam: Mapped["Exam"] = relationship(back_populates="sessions")
    candidate: Mapped["Candidate"] = relationship(back_populates="sessions")
    node: Mapped["Node"] = relationship(back_populates="sessions")
    responses: Mapped[list["Response"]] = relationship(back_populates="session")
    events: Mapped[list["Event"]] = relationship(back_populates="session")
    incident_links: Mapped[list["IncidentSession"]] = relationship(
        back_populates="session"
    )


class Response(Base):
    """Current-state projection of a candidate's answer; the Event ledger
    remains the historical source of truth."""

    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint("session_id", "question_id", name="uq_responses_session_question"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id"), nullable=False
    )
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), nullable=False
    )
    current_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    session: Mapped["Session"] = relationship(back_populates="responses")
    question: Mapped["Question"] = relationship(back_populates="responses")
