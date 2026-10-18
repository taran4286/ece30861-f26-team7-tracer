"""Time helpers. Every date rule uses today(session); stored timestamps use utcnow()."""

from datetime import UTC, date, datetime

from app.models import TestSession


def utcnow() -> datetime:
    """Real server time in UTC, for imported_at, recorded_at and audit timestamps."""
    return datetime.now(UTC)


def today(session: TestSession) -> date:
    """The spec's "today": the UTC date of the session's test_now."""
    return session.test_now.astimezone(UTC).date()
