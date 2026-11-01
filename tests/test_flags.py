"""app/services/flags.py through the Incident every read returns: at_risk, warning and overdue."""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.evidence import import_evidence
from app.services.filings import record_filing
from app.services.incidents import create_incident, get_incident
from app.services.workflow import list_audit_entries, record_determination, transition_incident
from tests.helpers import APPROVER, RESPONDER, change_session, in_session, new_session, ticket


def move_clock(session_id: str, day: str, clock: str = "15:00:00") -> None:
    test_now = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=UTC)
    change_session(session_id, test_now=test_now)


def flags(session_id: str, incident_id: str) -> list[str]:
    return get_incident(incident_id, **in_session(RESPONDER, session_id))["flags"]


def undetermined(session_id: str, discovery_date: str = "2026-10-01") -> str:
    created = create_incident({"title": "Breach", "discovery_date": discovery_date},
                              **in_session(RESPONDER, session_id))
    return created["incident_id"]


def determined(session_id: str, determination_date: str, decision: str = "Material") -> str:
    """A determined incident, with the session's clock moved to the determination date."""
    move_clock(session_id, determination_date)
    incident_id = undetermined(session_id, determination_date)
    imported = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]},
                               **in_session(RESPONDER, session_id))
    record_determination(incident_id, {
        "decision": decision, "determination_date": determination_date, "rationale": "Assessed",
        "evidence_refs": [imported["evidence"][0]["evidence_id"]], "version": 1,
    }, **in_session(APPROVER, session_id))
    return incident_id


def file_form(session_id: str, incident_id: str, filing_date: str, form: str = "8-K Item 1.05",
              **fields: Any) -> dict[str, Any]:
    body = {"form_type": form, "filing_date": filing_date,
            "accession_number": "0001234567-26-000001", "version": 1, **fields}
    return record_filing(incident_id, body, **in_session(APPROVER, session_id))


def amend(session_id: str, incident_id: str, info_available_date: str) -> None:
    transition_incident(incident_id, {
        "to": "AmendmentPending", "version": 1, "info_available_date": info_available_date,
        "elements": ["impact"],
    }, **in_session(APPROVER, session_id))


@pytest.mark.parametrize("today,expected", [
    ("2026-10-05", []),            # The determination date: four business days remain.
    ("2026-10-07", []),            # Wednesday: Thursday and Friday remain.
    ("2026-10-08", ["warning"]),   # Thursday: only Friday remains.
    ("2026-10-09", ["warning"]),   # The due date: none remain, and it isn't past.
    ("2026-10-10", ["overdue"]),   # Saturday, after the due date.
    ("2026-10-13", ["overdue"]),
])
def test_ac12_notices_for_an_incident_due_friday(today, expected):
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    move_clock(session_id, today)
    incident = get_incident(incident_id, **in_session(RESPONDER, session_id))
    assert incident["due_date"] == "2026-10-09"
    assert incident["flags"] == expected


@pytest.mark.parametrize("determination,today,expected", [
    # Due Monday 10-26: the weekend isn't counted.
    ("2026-10-20", "2026-10-22", []),
    ("2026-10-20", "2026-10-23", ["warning"]),
    ("2026-10-20", "2026-10-24", ["warning"]),
    # Due Tuesday 10-13: Monday 10-12 is Columbus Day, so Friday has one business day left.
    ("2026-10-06", "2026-10-08", []),
    ("2026-10-06", "2026-10-09", ["warning"]),
])
def test_warning_counts_only_business_days(determination, today, expected):
    session_id = new_session()
    incident_id = determined(session_id, determination)
    move_clock(session_id, today)
    assert flags(session_id, incident_id) == expected


@pytest.mark.parametrize("today,expected", [
    ("2026-10-07", []), ("2026-10-08", ["warning"]), ("2026-10-10", ["overdue"]),
])
def test_due_date_notices_apply_while_disclosure_is_delayed(today, expected):
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    transition_incident(incident_id, {"to": "DisclosureDelayed", "version": 2},
                        **in_session(APPROVER, session_id))
    move_clock(session_id, today)
    assert flags(session_id, incident_id) == expected


@pytest.mark.parametrize("today", ["2026-10-08", "2026-10-10"])
def test_recording_a_filing_clears_the_notice(today):
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    move_clock(session_id, today)
    assert flags(session_id, incident_id)
    assert file_form(session_id, incident_id, today)["flags"] == []
    move_clock(session_id, "2026-12-01")
    assert flags(session_id, incident_id) == []


def test_an_amendment_gets_its_own_notices_until_it_is_filed():
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    file_form(session_id, incident_id, "2026-10-05", unavailable_elements=["impact"])
    # Due Monday 10-26. The original 8-K's filing doesn't count for the 8-K/A.
    move_clock(session_id, "2026-10-20")
    amend(session_id, incident_id, "2026-10-20")
    assert flags(session_id, incident_id) == []
    move_clock(session_id, "2026-10-23")
    assert flags(session_id, incident_id) == ["warning"]
    move_clock(session_id, "2026-10-27")
    assert flags(session_id, incident_id) == ["overdue"]
    assert file_form(session_id, incident_id, "2026-10-27", "8-K/A")["flags"] == []


def test_a_second_amendment_is_not_cleared_by_the_first_ones_filing():
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    file_form(session_id, incident_id, "2026-10-05", unavailable_elements=["impact"])
    move_clock(session_id, "2026-10-20")
    amend(session_id, incident_id, "2026-10-20")
    file_form(session_id, incident_id, "2026-10-20", "8-K/A", unavailable_elements=["impact"])
    assert flags(session_id, incident_id) == []
    move_clock(session_id, "2026-11-02")
    amend(session_id, incident_id, "2026-11-02")
    move_clock(session_id, "2026-11-20")
    assert flags(session_id, incident_id) == ["overdue"]


@pytest.mark.parametrize("registrant,decision", [("FPI", "Material"), ("Domestic", "NotMaterial")])
def test_no_due_date_means_no_due_date_notice(registrant, decision):
    session_id = new_session(registrant_type=registrant)
    incident_id = determined(session_id, "2026-10-05", decision)
    move_clock(session_id, "2027-06-01")
    assert flags(session_id, incident_id) == []


@pytest.mark.parametrize("clock,expected", [
    ("2026-10-05T00:00:00Z", []),            # Exactly 7 days, which isn't more than 7.
    ("2026-10-05T00:00:01Z", ["at_risk"]),
    ("2026-10-04T23:59:59Z", []),
    ("2026-09-20T00:00:00Z", []),            # A clock before discovery.
])
def test_at_risk_is_measured_from_midnight_on_the_discovery_date(clock, expected):
    session_id = new_session()
    move_clock(session_id, "2026-09-28")
    incident_id = undetermined(session_id, "2026-09-28")
    move_clock(session_id, *clock.rstrip("Z").split("T"))
    assert flags(session_id, incident_id) == expected


def test_at_risk_uses_the_sessions_threshold():
    session_id = new_session(at_risk_threshold_days=2)
    incident_id = undetermined(session_id, "2026-10-03")
    assert flags(session_id, incident_id) == ["at_risk"]
    change_session(session_id, at_risk_threshold_days=3)
    assert flags(session_id, incident_id) == []


def test_the_largest_threshold_reads_without_overflowing():
    session_id = new_session(at_risk_threshold_days=2**31 - 1)
    assert flags(session_id, undetermined(session_id)) == []


def test_a_determination_clears_at_risk():
    session_id = new_session()
    move_clock(session_id, "2026-09-01")
    incident_id = undetermined(session_id, "2026-09-01")
    imported = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]},
                               **in_session(RESPONDER, session_id))
    move_clock(session_id, "2026-09-30")
    assert flags(session_id, incident_id) == ["at_risk"]
    result = record_determination(incident_id, {
        "decision": "NotMaterial", "determination_date": "2026-09-30", "rationale": "Assessed",
        "evidence_refs": [imported["evidence"][0]["evidence_id"]], "version": 1,
    }, **in_session(APPROVER, session_id))
    assert result["flags"] == []


def test_a_clock_far_before_the_deadline_reads_without_a_calendar_error():
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    move_clock(session_id, "0001-01-02", "00:00:00")
    assert flags(session_id, incident_id) == []


def test_flags_are_computed_on_read_and_write_nothing():
    session_id = new_session()
    incident_id = determined(session_id, "2026-10-05")
    who = in_session(RESPONDER, session_id)
    entries = list_audit_entries(incident_id, **who)
    before = get_incident(incident_id, **who)
    move_clock(session_id, "2026-10-10")
    after = get_incident(incident_id, **who)
    assert after["flags"] == ["overdue"]
    assert {**after, "flags": []} == before
    assert list_audit_entries(incident_id, **who) == entries
