"""An incident's flags (the system spec's "Flags"), computed on every read and never stored."""

from datetime import UTC, date, datetime, time, timedelta

from app.clock import today
from app.models import Filing, Flag, Incident, State, TestSession
from app.services.deadlines import is_business_day


def incident_flags(incident: Incident, filings: list[Filing], session: TestSession) -> list[str]:
    """The Flag values that apply at the session's test_now."""
    flags = []
    if _at_risk(incident, session):
        flags.append(Flag.AT_RISK.value)
    if incident.due_date is not None and not _filed(incident, filings):
        current = today(session)
        if current > incident.due_date:
            flags.append(Flag.OVERDUE.value)
        elif _due_within_one_business_day(current, incident.due_date):
            flags.append(Flag.WARNING.value)
    return flags


def _at_risk(incident: Incident, session: TestSession) -> bool:
    """UnderAssessment for more than the threshold, counted from 00:00 UTC on discovery_date."""
    if incident.state != State.UNDER_ASSESSMENT:
        return False
    start = datetime.combine(incident.discovery_date, time.min, UTC)
    # Divide rather than build a timedelta: the largest threshold a session stores overflows one.
    return (session.test_now - start) / timedelta(days=1) > session.at_risk_threshold_days


def _filed(incident: Incident, filings: list[Filing]) -> bool:
    """Whether the current required form has been filed.

    Recording a filing leaves AmendmentPending, so an open amendment is never filed, even when an
    earlier amendment already filed the same 8-K/A form.
    """
    if incident.state == State.AMENDMENT_PENDING:
        return False
    return any(filing.form_type == incident.required_form for filing in filings)


def _due_within_one_business_day(current: date, due_date: date) -> bool:
    """Whether at most one business day falls after current, through due_date.

    Counts back from due_date and stops at the second, so a session clock far before the deadline,
    even outside the holiday calendar's range, never reaches an unsupported year.
    """
    remaining = 0
    day = due_date
    while day > current:
        remaining += is_business_day(day)
        if remaining > 1:
            return False
        day -= timedelta(days=1)
    return True
