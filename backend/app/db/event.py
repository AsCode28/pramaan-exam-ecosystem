"""ORM model for the Event ledger.

The Event table is the most important table: it is append-only and
``sequence_no`` (``INTEGER PRIMARY KEY AUTOINCREMENT``) is the
authoritative event ordering. ``client_timestamp`` is diagnostic metadata
only and must never be used for canonical ordering.

Candidate-originated events support idempotency through
``UNIQUE(session_id, client_event_id)``.

``previous_hash`` / ``hash`` are schema placeholders only; the hash-chain
service is a separate later task, so both are nullable here and no hash
generation is implemented.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.exam import Exam, Node
    from app.db.candidate import Candidate
    from app.db.session import Session


def _utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone-aware DATETIME)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "session_id", "client_event_id", name="uq_events_session_client"
        ),
        Index("ix_events_session_id", "session_id"),
        Index("ix_events_exam_id", "exam_id"),
        Index("ix_events_node_server_ts", "node_id", "server_timestamp"),
        Index("ix_events_server_ts", "server_timestamp"),
        # Render the literal AUTOINCREMENT keyword: sequence_no must be
        # `INTEGER PRIMARY KEY AUTOINCREMENT` on SQLite.
        {"sqlite_autoincrement": True},
    )

    # Authoritative ordering: renders as INTEGER PRIMARY KEY AUTOINCREMENT
    # on SQLite. Never use client_timestamp for canonical ordering.
    sequence_no: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    client_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Nullable: server / exam-level events may not belong to a candidate
    # session. exam_id below attributes every event to its examination.
    exam_id: Mapped[int | None] = mapped_column(
        ForeignKey("exams.id"), nullable=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("sessions.id"), nullable=True
    )
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id"), nullable=True
    )
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("nodes.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    client_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    server_timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    # Hash-chain placeholders; populated by the future hash-chain service.
    previous_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    session: Mapped["Session | None"] = relationship(back_populates="events")
    candidate: Mapped["Candidate | None"] = relationship()
    node: Mapped["Node | None"] = relationship(back_populates="events")
    exam: Mapped["Exam | None"] = relationship(back_populates="events")
