"""Time helpers. Every date rule uses today(); stored timestamps use utcnow()."""

from datetime import UTC, date, datetime


def utcnow() -> datetime:
    """Real server time in UTC, for imported_at, recorded_at and audit timestamps."""
    return datetime.now(UTC)


def today() -> date:
    """The current UTC date. Switches to the run's test_now once test control is added."""
    return utcnow().date()
