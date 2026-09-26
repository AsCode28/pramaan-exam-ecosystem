"""Domain errors for the session/demo APIs.

Raised by service-layer code; mapped to HTTP status codes by the routers.
Keeping them here avoids a services -> api import.
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


class NodeUnavailable(SessionApiError):
    """Assigned node is FAILED; the session cannot accept the operation."""

    def __init__(self, node_id: int, session_id: int | None, operation: str):
        super().__init__(
            f"assigned node {node_id} is FAILED; session {session_id} "
            f"cannot accept {operation}"
        )
        self.node_id = node_id
        self.session_id = session_id
        self.operation = operation


class InvalidNodeState(SessionApiError):
    """Node is in a status that cannot transition to FAILED."""

    def __init__(self, node_id: int, status: str):
        super().__init__(
            f"node {node_id} status {status!r} cannot transition to FAILED"
        )
        self.node_id = node_id
        self.status = status
