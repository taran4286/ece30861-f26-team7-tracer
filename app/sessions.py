"""The test session a request names in X-Test-Run, which scopes every project lookup."""

from sqlalchemy.orm import Session

from app.errors import ErrorCode, TracerError
from app.models import TestSession


def load_session(tx: Session, session_id: str, *, for_update: bool = False) -> TestSession:
    """The request's test session, or NOT_FOUND. `for_update=True` locks its row."""
    # PostgreSQL text can't hold NUL, so no stored ID has one, and querying for it would fail.
    session = None if "\x00" in session_id else tx.get(
        TestSession, session_id, with_for_update=for_update
    )
    if session is None:
        raise TracerError(ErrorCode.NOT_FOUND, f"Test session {session_id!r} not found")
    return session
