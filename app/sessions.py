"""The test session a request names in X-Test-Run, which scopes every project lookup."""

from sqlalchemy.orm import Session

from app.errors import ErrorCode, TracerError
from app.models import TestSession


def load_session(tx: Session, session_id: str) -> TestSession:
    """The request's test session, or NOT_FOUND."""
    session = tx.get(TestSession, session_id)
    if session is None:
        raise TracerError(ErrorCode.NOT_FOUND, f"Test session {session_id!r} not found")
    return session
