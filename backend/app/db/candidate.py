"""ORM model for candidates.

Persistence layer only (SQLAlchemy). Separate from the Pydantic
``Candidate`` validation model in ``app.models``.
"""

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.session import Session


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    roll_no: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)

    sessions: Mapped[list["Session"]] = relationship(back_populates="candidate")
