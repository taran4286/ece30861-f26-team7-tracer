from datetime import UTC, datetime, timedelta

import pytest

from app.clock import utcnow
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, State
from app.services.evidence import import_evidence
from app.services.incidents import get_incident
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    SESSION_ID,
    alert,
    audit_entries,
    in_session,
    new_session,
    set_state,
    table_counts,
    ticket,
)

pytestmark = pytest.mark.usefixtures("default_session")


def _rejected(incident_id, body, code=ErrorCode.VALIDATION_ERROR, field=None, who=RESPONDER):
    before = table_counts()
    with pytest.raises(TracerError) as err:
        import_evidence(incident_id, body, **who)
    assert err.value.code == code
    assert err.value.field == field
    assert table_counts() == before


def test_ticketing_item_is_normalized(incident):
    raw = ticket(7, created_at="2026-10-01T08:30:00-04:00", priority="P1")
    [item] = import_evidence(
        incident["incident_id"], {"source": "ticketing", "items": [raw]}, **RESPONDER
    )["evidence"]
    assert item["evidence_id"].startswith("ev_")
    assert item["source"] == "ticketing"
    assert item["source_ref"] == "TCK-7"
    assert item["summary"] == "Ticket 7"
    assert datetime.fromisoformat(item["observed_at"]) == datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    assert item["imported_by"] == RESPONDER["user_id"]
    assert item["impact_categories"] == []


def test_siem_item_is_normalized(incident):
    [item] = import_evidence(
        incident["incident_id"], {"source": "siem", "items": [alert(3)]}, **APPROVER
    )["evidence"]
    assert item["source"] == "siem"
    assert item["source_ref"] == "ALR-3"
    assert item["summary"] == "Rule 3"
    assert datetime.fromisoformat(item["observed_at"]) == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert item["imported_by"] == APPROVER["user_id"]


def test_imported_at_is_server_time(incident):
    [item] = import_evidence(
        incident["incident_id"], {"source": "ticketing", "items": [ticket()]}, **RESPONDER
    )["evidence"]
    assert abs(datetime.fromisoformat(item["imported_at"]) - utcnow()) < timedelta(seconds=5)


def test_twenty_tickets_and_twenty_alerts_make_forty_items(incident):
    incident_id = incident["incident_id"]
    tickets = import_evidence(
        incident_id, {"source": "ticketing", "items": [ticket(n) for n in range(20)]}, **RESPONDER
    )["evidence"]
    alerts = import_evidence(
        incident_id, {"source": "siem", "items": [alert(n) for n in range(20)]}, **RESPONDER
    )["evidence"]
    # Returned in input order.
    assert [e["source_ref"] for e in tickets] == [f"TCK-{n}" for n in range(20)]
    assert [e["source_ref"] for e in alerts] == [f"ALR-{n}" for n in range(20)]
    stored = get_incident(incident_id, **BOARD)["evidence"]
    assert len(stored) == 40
    assert {e["evidence_id"] for e in stored} == {e["evidence_id"] for e in tickets + alerts}


@pytest.mark.parametrize(
    ("source", "items", "field"),
    [
        ("ticketing", [ticket(0), ticket(1, ticket_id=None)], "items[1].ticket_id"),
        ("ticketing", [ticket(0), {"summary": "x", "created_at": "2026-10-01T00:00:00Z"}],
         "items[1].ticket_id"),
        ("ticketing", [ticket(0), ticket(1, summary="")], "items[1].summary"),
        ("ticketing", [ticket(0), ticket(1, created_at="yesterday")], "items[1].created_at"),
        ("ticketing", [ticket(0), ticket(1, created_at="2026-10-01T12:00:00")],
         "items[1].created_at"),
        ("siem", [alert(0), alert(1), alert(2), alert(3, severity="urgent")], "items[3].severity"),
        ("siem", [alert(0), alert(1, severity=None)], "items[1].severity"),
        ("siem", [alert(0), alert(1, rule_name=7)], "items[1].rule_name"),
        ("siem", [alert(0), alert(1), alert(2), alert(3, detected_at="2026-13-01T00:00:00Z")],
         "items[3].detected_at"),
        # Shaped for the other source.
        ("ticketing", [ticket(0), alert(1)], "items[1].ticket_id"),
        ("siem", [alert(0), ticket(1)], "items[1].alert_id"),
        ("ticketing", [ticket(0), ticket(1, alert_id="ALR-1")], "items[1].alert_id"),
        ("siem", [alert(0), alert(1, ticket_id="TCK-1")], "items[1].ticket_id"),
        ("siem", [alert(0), "not an object"], "items[1]"),
    ],
)
def test_one_bad_item_rejects_the_whole_batch(incident, source, items, field):
    _rejected(incident["incident_id"], {"source": source, "items": items}, field=field)
    assert get_incident(incident["incident_id"], **BOARD)["evidence"] == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"items": [ticket()]}, "source"),
        ({"source": "email", "items": [ticket()]}, "source"),
        ({"source": "siem"}, "items"),
        ({"source": "siem", "items": []}, "items"),
        ({"source": "siem", "items": alert()}, "items"),
        ("not an object", None),
    ],
)
def test_bad_request_body_is_rejected(incident, body, field):
    _rejected(incident["incident_id"], body, field=field)


def test_board_cannot_import(incident):
    _rejected(
        incident["incident_id"],
        {"source": "siem", "items": [alert()]},
        code=ErrorCode.FORBIDDEN_ROLE,
        who=BOARD,
    )


def test_unknown_incident_is_not_found_before_the_body_is_checked():
    _rejected("inc_missing", {"source": "email"}, code=ErrorCode.NOT_FOUND)


def test_role_is_checked_before_the_incident():
    _rejected("inc_missing", {}, code=ErrorCode.FORBIDDEN_ROLE, who=BOARD)


def test_unknown_session_is_not_found_before_the_role():
    _rejected("inc_missing", {}, code=ErrorCode.NOT_FOUND, who=in_session(BOARD, "pytest-missing"))


def test_another_sessions_incident_is_not_found(incident):
    _rejected(
        incident["incident_id"],
        {"source": "siem", "items": [alert()]},
        code=ErrorCode.NOT_FOUND,
        who=in_session(RESPONDER, new_session()),
    )
    assert get_incident(incident["incident_id"], **BOARD)["evidence"] == []


@pytest.mark.parametrize("state", list(State))
def test_import_works_in_every_state_and_keeps_the_version(incident, state):
    incident_id = incident["incident_id"]
    set_state(incident_id, state)
    import_evidence(incident_id, {"source": "siem", "items": [alert()]}, **RESPONDER)
    after = get_incident(incident_id, **BOARD)
    assert after["state"] == state.value
    assert after["version"] == 1
    assert after["determination"] is None
    assert len(after["evidence"]) == 1


def test_each_import_writes_one_audit_entry(incident):
    incident_id = incident["incident_id"]
    first = import_evidence(
        incident_id, {"source": "ticketing", "items": [ticket(1), ticket(2)]}, **RESPONDER
    )["evidence"]
    second = import_evidence(incident_id, {"source": "siem", "items": [alert(1)]}, **APPROVER)[
        "evidence"
    ]
    entries = [e for e in audit_entries(incident_id) if e.action == AuditAction.IMPORT_EVIDENCE]
    assert [(e.session_id, e.user_id, e.before, e.after) for e in entries] == [
        (
            SESSION_ID,
            RESPONDER["user_id"],
            None,
            {"source": "ticketing", "evidence_ids": [e["evidence_id"] for e in first]},
        ),
        (
            SESSION_ID,
            APPROVER["user_id"],
            None,
            {"source": "siem", "evidence_ids": [e["evidence_id"] for e in second]},
        ),
    ]
