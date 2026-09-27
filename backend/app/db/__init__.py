"""SQLAlchemy ORM layer for PRAMAAN.

Importing this package registers every ORM model on the shared ``Base``
metadata, so ``Base.metadata.create_all()`` always creates all 9 tables.
``app.core.database`` imports ``Base`` through this package (not through
``app.db.base``) to guarantee that registration happens before any table
creation executes.

This layer is persistence only. It never imports from ``app.models``
(Pydantic validation models); translation between the two, if ever needed,
is a separate later concern.
"""

from app.db import candidate as candidate  # noqa: F401
from app.db import event as event  # noqa: F401
from app.db import exam as exam  # noqa: F401
from app.db import incident as incident  # noqa: F401
from app.db import session as session  # noqa: F401
from app.db.base import Base
from app.db.candidate import Candidate
from app.db.event import Event
from app.db.exam import Exam, Node, NodeStatus, Question
from app.db.incident import Incident, IncidentSession
from app.db.session import Response, Session

__all__ = [
    "Base",
    "Candidate",
    "Event",
    "Exam",
    "Incident",
    "IncidentSession",
    "Node",
    "NodeStatus",
    "Question",
    "Response",
    "Session",
]
