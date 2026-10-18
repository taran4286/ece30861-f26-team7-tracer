from datetime import datetime, timedelta

import pytest

from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Incident
from app.services.incidents import create_incident, get_incident
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    SESSION_ID,
    TODAY,
    audit_entries,
    in_session,
    new_session,
    table_counts,
)

pytestmark = pytest.mark.usefixtures("default_session")


def _rejected(body, code=ErrorCode.VALIDATION_ERROR, field=None, who=RESPONDER):
    before = table_counts()
    with pytest.raises(TracerError) as err:
        create_incident(body, **who)
    assert err.value.code == code
    assert err.value.field == field
    assert table_counts() == before


def test_new_incident_has_defaults(incident):
    assert incident["incident_id"].startswith("inc_")
    assert incident == {
        "incident_id": incident["incident_id"],
        "title": "Ransomware",
        "discovery_date": "2026-10-01",
        "state": "UnderAssessment",
        "determination": None,
        "required_form": None,
        "due_date": None,
        "flags": [],
        "filings": [],
        "evidence": [],
        "group_id": None,
        "version": 1,
    }


def test_new_incident_belongs_to_the_session(incident):
    with transaction() as tx:
        assert tx.get(Incident, incident["incident_id"]).session_id == SESSION_ID


def test_get_returns_the_stored_incident_for_every_role(incident):
    for who in (RESPONDER, APPROVER, BOARD):
        assert get_incident(incident["incident_id"], **who) == incident


def test_get_unknown_incident_is_not_found():
    with pytest.raises(TracerError) as err:
        get_incident("inc_missing", **BOARD)
    assert err.value.code == ErrorCode.NOT_FOUND


def test_get_with_unknown_role_is_forbidden(incident):
    with pytest.raises(TracerError) as err:
        get_incident(incident["incident_id"], user_id="u", role="Auditor", session_id=SESSION_ID)
    assert err.value.code == ErrorCode.FORBIDDEN_ROLE


def test_another_sessions_incident_is_not_found(incident):
    other = new_session()
    with pytest.raises(TracerError) as err:
        get_incident(incident["incident_id"], **in_session(BOARD, other))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_unknown_session_is_not_found_before_the_role():
    _rejected({}, code=ErrorCode.NOT_FOUND, who=in_session(BOARD, "pytest-missing"))
    with pytest.raises(TracerError) as err:
        get_incident("inc_missing", **in_session(BOARD, "pytest-missing"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_hundred_incidents_get_distinct_ids():
    ids = {
        create_incident({"title": f"Incident {n}", "discovery_date": "2026-10-01"}, **RESPONDER)[
            "incident_id"
        ]
        for n in range(100)
    }
    assert len(ids) == 100


@pytest.mark.parametrize(
    "body",
    [
        {"discovery_date": "2026-10-01"},
        {"title": "", "discovery_date": "2026-10-01"},
        {"title": "   ", "discovery_date": "2026-10-01"},
        {"title": None, "discovery_date": "2026-10-01"},
        {"title": 42, "discovery_date": "2026-10-01"},
    ],
)
def test_missing_or_empty_title_is_rejected(body):
    _rejected(body, field="title")


@pytest.mark.parametrize(
    "discovery_date",
    [None, "", "2026-02-30", "20261001", "2026-10-01T00:00:00Z", "01/10/2026", 20261001],
)
def test_missing_or_malformed_discovery_date_is_rejected(discovery_date):
    body = {"title": "Phishing"}
    if discovery_date is not None:
        body["discovery_date"] = discovery_date
    _rejected(body, field="discovery_date")


def test_discovery_date_after_today_is_rejected():
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    _rejected({"title": "Phishing", "discovery_date": tomorrow}, field="discovery_date")


def test_discovery_date_equal_to_today_is_accepted():
    created = create_incident(
        {"title": "Phishing", "discovery_date": TODAY.isoformat()}, **APPROVER
    )
    assert created["discovery_date"] == TODAY.isoformat()


def test_today_is_the_date_of_the_sessions_test_now():
    # 23:30 at -04:00 is already 2027-01-11 in UTC.
    test_now = datetime.fromisoformat("2027-01-10T23:30:00-04:00")
    later = in_session(RESPONDER, new_session(test_now=test_now))
    body = {"title": "Phishing", "discovery_date": "2027-01-11"}
    assert create_incident(body, **later)["discovery_date"] == "2027-01-11"
    _rejected({**body, "discovery_date": "2027-01-12"}, field="discovery_date", who=later)
    # The default session's today is still 2026-10-05.
    _rejected(body, field="discovery_date")


def test_non_object_body_is_rejected():
    _rejected(["title", "discovery_date"], field=None)


def test_board_cannot_create_an_incident():
    _rejected(
        {"title": "Phishing", "discovery_date": "2026-10-01"},
        code=ErrorCode.FORBIDDEN_ROLE,
        who=BOARD,
    )


def test_role_is_checked_before_the_body():
    _rejected({}, code=ErrorCode.FORBIDDEN_ROLE, who=BOARD)


def test_each_create_writes_one_audit_entry(incident):
    [entry] = audit_entries(incident["incident_id"])
    assert entry.action == AuditAction.CREATE_INCIDENT
    assert entry.session_id == SESSION_ID
    assert entry.user_id == RESPONDER["user_id"]
    assert entry.before is None
    assert entry.after == {
        "state": "UnderAssessment",
        "title": "Ransomware",
        "discovery_date": "2026-10-01",
        "version": 1,
    }
