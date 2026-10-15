from datetime import timedelta

import pytest

from app.clock import today
from app.errors import ErrorCode, TracerError
from app.models import AuditAction
from app.services.incidents import create_incident, get_incident
from tests.helpers import APPROVER, BOARD, RESPONDER, audit_entries, table_counts


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


def test_get_returns_the_stored_incident_for_every_role(incident):
    for who in (RESPONDER, APPROVER, BOARD):
        assert get_incident(incident["incident_id"], **who) == incident


def test_get_unknown_incident_is_not_found():
    with pytest.raises(TracerError) as err:
        get_incident("inc_missing", **BOARD)
    assert err.value.code == ErrorCode.NOT_FOUND


def test_get_with_unknown_role_is_forbidden(incident):
    with pytest.raises(TracerError) as err:
        get_incident(incident["incident_id"], user_id="u", role="Auditor")
    assert err.value.code == ErrorCode.FORBIDDEN_ROLE


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
    tomorrow = (today() + timedelta(days=1)).isoformat()
    _rejected({"title": "Phishing", "discovery_date": tomorrow}, field="discovery_date")


def test_discovery_date_equal_to_today_is_accepted():
    created = create_incident(
        {"title": "Phishing", "discovery_date": today().isoformat()}, **APPROVER
    )
    assert created["discovery_date"] == today().isoformat()


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
    assert entry.user_id == RESPONDER["user_id"]
    assert entry.before is None
    assert entry.after == {
        "state": "UnderAssessment",
        "title": "Ransomware",
        "discovery_date": "2026-10-01",
        "version": 1,
    }
