"""ORM models for incidents and the incident<->session join table.

Persistence layer only (SQLAlchemy). No incident engine, detection, or
recovery logic lives here -- schema only.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.event import Event
    from app.db.exam import Exam
    from app.db.session import Session


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id"), nullable=False)
    severity: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    root_cause_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_from_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.sequence_no"), nullable=True
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    exam: Mapped["Exam"] = relationship(back_populates="incidents")
    origin_event: Mapped["Event | None"] = relationship()
    session_links: Mapped[list["IncidentSession"]] = relationship(
        back_populates="incident"
    )


class IncidentSession(Base):
    """Join table linking incidents to affected sessions.

    Composite primary key (incident_id, session_id).
    """

    __tablename__ = "incident_sessions"

    incident_id: Mapped[int] = mapped_column(
        ForeignKey("incidents.id"), primary_key=True
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id"), primary_key=True
    )

    incident: Mapped["Incident"] = relationship(back_populates="session_links")
    session: Mapped["Session"] = relationship(back_populates="incident_links")
