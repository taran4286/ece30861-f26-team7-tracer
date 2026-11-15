from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from app.clock import utcnow
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, ImpactCategory, State
from app.services.evidence import import_evidence, tag_evidence
from app.services.incidents import create_incident, get_incident
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
    assert get_incident(incident["incident_id"], **BOARD) == incident


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


# --- Tagging (REQ-05) ----------------------------------------------------------------------

ALL_CATEGORIES = [category.value for category in ImpactCategory]


@pytest.fixture
def evidence_id(incident) -> str:
    imported = import_evidence(
        incident["incident_id"], {"source": "siem", "items": [alert()]}, **RESPONDER
    )
    return imported["evidence"][0]["evidence_id"]


def _item(incident_id, evidence_id):
    [item] = [e for e in get_incident(incident_id, **BOARD)["evidence"]
              if e["evidence_id"] == evidence_id]
    return item


def _tag_rejected(incident_id, evidence_id, body, code=ErrorCode.VALIDATION_ERROR, field=None,
                  who=RESPONDER):
    """A rejected tag stores nothing and leaves a known incident and its evidence as they were."""
    previous = None if incident_id == "inc_missing" else get_incident(incident_id, **BOARD)
    before = table_counts()
    with pytest.raises(TracerError) as err:
        tag_evidence(incident_id, evidence_id, body, **who)
    assert err.value.code == code
    assert err.value.field == field
    assert table_counts() == before
    if previous is not None:
        assert get_incident(incident_id, **BOARD) == previous


@pytest.mark.parametrize("who", [RESPONDER, APPROVER])
def test_tagging_stores_all_five_categories(incident, evidence_id, who):
    incident_id = incident["incident_id"]
    untagged = _item(incident_id, evidence_id)
    item = tag_evidence(incident_id, evidence_id, {"impact_categories": ALL_CATEGORIES}, **who)
    assert item == {**untagged, "impact_categories": ALL_CATEGORIES}
    assert _item(incident_id, evidence_id) == item


def test_tagging_replaces_the_categories_in_the_order_sent(incident, evidence_id):
    incident_id = incident["incident_id"]
    body = {"impact_categories": ["legal_regulatory", "operational"]}
    assert tag_evidence(incident_id, evidence_id, body, **RESPONDER)["impact_categories"] == [
        "legal_regulatory", "operational"]
    tag_evidence(incident_id, evidence_id, {"impact_categories": ["reputational"]}, **RESPONDER)
    assert _item(incident_id, evidence_id)["impact_categories"] == ["reputational"]
    # An empty array clears them.
    tag_evidence(incident_id, evidence_id, {"impact_categories": []}, **RESPONDER)
    assert _item(incident_id, evidence_id)["impact_categories"] == []


def test_tagging_changes_only_that_item_and_ignores_other_fields(incident):
    incident_id = incident["incident_id"]
    first, second = import_evidence(
        incident_id, {"source": "ticketing", "items": [ticket(1), ticket(2)]}, **RESPONDER
    )["evidence"]
    before = get_incident(incident_id, **BOARD)
    tag_evidence(incident_id, second["evidence_id"],
                 {"impact_categories": ["financial"], "summary": "ignored"}, **RESPONDER)
    after = get_incident(incident_id, **BOARD)
    assert _item(incident_id, first["evidence_id"]) == first
    assert _item(incident_id, second["evidence_id"]) == {**second, "impact_categories": ["financial"]}
    assert {k: v for k, v in after.items() if k != "evidence"} == {
        k: v for k, v in before.items() if k != "evidence"}


@pytest.mark.parametrize("state", list(State))
def test_tagging_works_in_every_state_and_keeps_the_version(incident, evidence_id, state):
    incident_id = incident["incident_id"]
    set_state(incident_id, state)
    tag_evidence(incident_id, evidence_id, {"impact_categories": ["operational"]}, **RESPONDER)
    after = get_incident(incident_id, **BOARD)
    assert after["state"] == state.value
    assert after["version"] == 1
    assert _item(incident_id, evidence_id)["impact_categories"] == ["operational"]


@pytest.mark.parametrize("body,field", [
    (None, None),
    ([], None),
    ({}, "impact_categories"),
    ({"impact_categories": None}, "impact_categories"),
    ({"impact_categories": "operational"}, "impact_categories"),
    ({"impact_categories": {"operational": True}}, "impact_categories"),
    # AC-05: a value outside the five categories.
    ({"impact_categories": ["misc"]}, "impact_categories[0]"),
    ({"impact_categories": ["operational", "misc"]}, "impact_categories[1]"),
    ({"impact_categories": ["Operational"]}, "impact_categories[0]"),
    ({"impact_categories": [1]}, "impact_categories[0]"),
    ({"impact_categories": [None]}, "impact_categories[0]"),
    ({"impact_categories": [["financial"]]}, "impact_categories[0]"),
    ({"impact_categories": ["financial", "operational", "financial"]}, "impact_categories[2]"),
])
def test_bad_tag_body_is_rejected_and_keeps_the_categories(incident, evidence_id, body, field):
    incident_id = incident["incident_id"]
    tag_evidence(incident_id, evidence_id, {"impact_categories": ["operational"]}, **RESPONDER)
    _tag_rejected(incident_id, evidence_id, body, field=field)
    assert _item(incident_id, evidence_id)["impact_categories"] == ["operational"]


def test_board_cannot_tag(incident, evidence_id):
    _tag_rejected(incident["incident_id"], evidence_id, {"impact_categories": ["financial"]},
                  code=ErrorCode.FORBIDDEN_ROLE, who=BOARD)


def test_tagging_checks_the_session_then_the_role_then_the_path_then_the_body(incident, evidence_id):
    incident_id = incident["incident_id"]
    bad = {"impact_categories": ["misc"]}
    _tag_rejected("inc_missing", "ev_missing", bad, code=ErrorCode.NOT_FOUND,
                  who=in_session(BOARD, "pytest-missing"))
    _tag_rejected("inc_missing", "ev_missing", bad, code=ErrorCode.FORBIDDEN_ROLE, who=BOARD)
    _tag_rejected("inc_missing", evidence_id, bad, code=ErrorCode.NOT_FOUND)
    _tag_rejected(incident_id, "ev_missing", bad, code=ErrorCode.NOT_FOUND)
    _tag_rejected(incident_id, "ev_\x00", bad, code=ErrorCode.NOT_FOUND)


def test_tagging_another_incidents_or_sessions_evidence_is_not_found(incident, evidence_id):
    body = {"impact_categories": ["financial"]}
    other = create_incident({"title": "Other", "discovery_date": "2026-10-01"}, **RESPONDER)
    _tag_rejected(other["incident_id"], evidence_id, body, code=ErrorCode.NOT_FOUND)
    _tag_rejected(incident["incident_id"], evidence_id, body, code=ErrorCode.NOT_FOUND,
                  who=in_session(RESPONDER, new_session()))
    assert _item(incident["incident_id"], evidence_id)["impact_categories"] == []


def test_each_tag_writes_one_audit_entry(incident, evidence_id):
    incident_id = incident["incident_id"]
    tag_evidence(incident_id, evidence_id, {"impact_categories": ["operational"]}, **RESPONDER)
    tag_evidence(incident_id, evidence_id, {"impact_categories": ["financial", "reputational"]},
                 **APPROVER)
    entries = [e for e in audit_entries(incident_id) if e.action == AuditAction.TAG_EVIDENCE]
    assert [(e.session_id, e.user_id, e.target_id, e.before, e.after) for e in entries] == [
        (SESSION_ID, RESPONDER["user_id"], incident_id,
         {"evidence_id": evidence_id, "impact_categories": []},
         {"evidence_id": evidence_id, "impact_categories": ["operational"]}),
        (SESSION_ID, APPROVER["user_id"], incident_id,
         {"evidence_id": evidence_id, "impact_categories": ["operational"]},
         {"evidence_id": evidence_id, "impact_categories": ["financial", "reputational"]}),
    ]


def test_competing_tags_each_record_what_they_replaced(incident, evidence_id):
    """The row lock makes the second tag wait, so its entry's before is the first one's after."""
    incident_id = incident["incident_id"]
    ready = Barrier(2)

    def submit(category):
        ready.wait(timeout=10)
        tag_evidence(incident_id, evidence_id, {"impact_categories": [category]}, **RESPONDER)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(submit, ["operational", "financial"]))
    first, second = [e for e in audit_entries(incident_id) if e.action == AuditAction.TAG_EVIDENCE]
    assert first.before["impact_categories"] == []
    assert second.before == first.after
    assert _item(incident_id, evidence_id)["impact_categories"] == second.after["impact_categories"]
