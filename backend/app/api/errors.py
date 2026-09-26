"""Domain errors for the session API.

Raised by ``app.services.session_service``; mapped to HTTP status codes by
``app.api.sessions``. Keeping them here avoids an services -> api import.
"""


class SessionApiError(Exception):
    """Base class for session domain errors."""


class NotFound(SessionApiError):
    """A referenced row (exam/candidate/node/session/question) is missing."""

    def __init__(self, entity: str):
        super().__init__(f"{entity} not found")
        self.entity = entity


class ForeignExamQuestion(SessionApiError):
    """Question belongs to a different exam than the session."""

    def __init__(self):
        super().__init__("question belongs to a different exam")


class InvalidState(SessionApiError):
    """Session is not in a state that allows the requested operation."""

    def __init__(self, status: str):
        super().__init__(f"session status {status!r} does not allow this operation")
        self.status = status
