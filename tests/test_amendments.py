"""Amendment requests, refiling, closure and the complete transition matrix."""

from datetime import UTC, datetime

import pytest

from app.errors import ErrorCode, TracerError
from app.models import State
from app.services import workflow
from app.services.deadlines import MAX_CALENDAR_YEAR
from app.services.filings import record_filing
from app.services.incidents import get_incident
from tests.helpers import APPROVER, BOARD, RESPONDER, audit_entries, in_session, new_session, set_state, table_counts
from tests.test_filing_storage import material_case

pytestmark = pytest.mark.usefixtures("default_session")

AMENDMENT = {"to": "AmendmentPending", "version": 3,
             "info_available_date": "2026-10-05", "elements": ["impact"]}


def recorded_case(registrant="Domestic", **settings):
    """Create a material incident and its initial filing through the real services."""
    incident_id, actor, body = material_case(registrant, **settings)
    record_filing(incident_id, body, **actor)
    return incident_id, actor, body


def rejected(incident_id, actor, body, code, field=None):
    """Rejected transitions leave existing values, filing rows and audit rows intact."""
    before, counts = get_incident(incident_id, **actor), table_counts()
    with pytest.raises(TracerError) as error:
        workflow.transition_incident(incident_id, body, **actor)
    assert (error.value.code, error.value.field) == (code, field)
    assert get_incident(incident_id, **actor) == before
    assert table_counts() == counts


@pytest.mark.parametrize("registrant,form,due", [
    ("Domestic", "8-K/A", "2026-10-09"), ("FPI", "6-K", None),
])
def test_amendment_changes_form_deadline_and_audit(registrant, form, due):
    """The session registrant controls the amendment form and four-business-day rule."""
    incident_id, actor, _ = recorded_case(registrant)
    before = get_incident(incident_id, **actor)
    entries = audit_entries(incident_id)
    result = workflow.transition_incident(incident_id, AMENDMENT, **actor)
    assert result == {**before, "state": "AmendmentPending", "version": 4,
                      "required_form": form, "due_date": due}
    assert result == get_incident(incident_id, **actor)
    saved = audit_entries(incident_id)
    assert len(saved) == len(entries) + 1
    entry = saved[-1]
    assert entry.action.value == "transition" and entry.user_id == actor["user_id"]
    assert entry.session_id == actor["session_id"]
    assert entry.before == {"state": "DisclosureRecorded", "version": 3,
                            "required_form": before["required_form"], "due_date": before["due_date"]}
    assert entry.after == {"state": "AmendmentPending", "version": 4,
                           "required_form": form, "due_date": due,
                           "info_available_date": "2026-10-05", "elements": ["impact"]}


# These are the transitions endpoint's pairs; filing/determination pairs use their own endpoints.
TRANSITION_PAIRS = {
    (State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
    (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
    (State.DETERMINED_NOT_MATERIAL, State.CLOSED),
    (State.DISCLOSURE_RECORDED, State.AMENDMENT_PENDING),
    (State.DISCLOSURE_RECORDED, State.CLOSED),
}


@pytest.mark.parametrize("source", list(State))
@pytest.mark.parametrize("target", list(State))
def test_every_transition_pair_with_valid_amendment_context(source, target):
    """All 49 pairs have an explicit result, including the two newly reachable states."""
    incident_id, actor, _ = recorded_case()
    set_state(incident_id, source)
    body = {**AMENDMENT, "to": target.value} if target == State.AMENDMENT_PENDING else {
        "to": target.value, "version": 1,
    }
    if (source, target) not in TRANSITION_PAIRS:
        rejected(incident_id, actor, body, ErrorCode.INVALID_TRANSITION)
        return
    before = get_incident(incident_id, **actor)
    result = workflow.transition_incident(incident_id, body, **actor)
    expected = {**before, "state": target.value, "version": before["version"] + 1}
    if target == State.AMENDMENT_PENDING:
        expected.update(required_form="8-K/A", due_date="2026-10-09")
    assert result == expected


@pytest.mark.parametrize("changes,field", [
    ({"info_available_date": None}, "info_available_date"),
    ({"info_available_date": "2026-10-06"}, "info_available_date"),
    ({"info_available_date": "2026-02-30"}, "info_available_date"),
    ({"elements": None}, "elements"), ({"elements": []}, "elements"),
    ({"elements": ["impact", "impact"]}, "elements[1]"),
    ({"elements": ["other"]}, "elements[0]"),
    ({"elements": ["scope"]}, "elements[0]"),
    ({"elements": ["impact", "nature"]}, "elements[1]"),
])
@pytest.mark.parametrize("source", [State.DISCLOSURE_RECORDED, State.CLOSED])
def test_invalid_amendment_body_precedes_state(changes, field, source):
    incident_id, actor, _ = recorded_case()
    set_state(incident_id, source)
    rejected(incident_id, actor, {**AMENDMENT, **changes}, ErrorCode.VALIDATION_ERROR, field)


def test_missing_latest_filing_cannot_start_amendment():
    incident_id, actor, _ = material_case()
    set_state(incident_id, State.DISCLOSURE_RECORDED)
    rejected(incident_id, actor, AMENDMENT, ErrorCode.INVALID_TRANSITION)


@pytest.mark.parametrize("registrant", ["Domestic", "FPI"])
def test_refiling_uses_new_form_and_only_latest_unavailable_elements(registrant):
    """Complete two amendment cycles; the old filing cannot authorize missing elements."""
    incident_id, actor, original_body = recorded_case(registrant)
    pending = workflow.transition_incident(incident_id, AMENDMENT, **actor)
    if registrant == "Domestic":
        rejected_body = {**original_body, "version": pending["version"]}
        counts = table_counts()
        with pytest.raises(TracerError) as error:
            record_filing(incident_id, rejected_body, **actor)
        assert error.value.field == "form_type"
        assert get_incident(incident_id, **actor) == pending and table_counts() == counts
    filed = record_filing(incident_id, {
        **original_body, "form_type": pending["required_form"], "version": 4,
        "accession_number": "0001234567-26-000002", "unavailable_elements": ["nature"],
    }, **actor)
    assert filed["state"] == "DisclosureRecorded" and filed["version"] == 5
    assert len(filed["filings"]) == 2
    rejected(incident_id, actor, AMENDMENT, ErrorCode.VALIDATION_ERROR, "elements[0]")
    next_pending = workflow.transition_incident(incident_id, {**AMENDMENT, "elements": ["nature"]}, **actor)
    assert next_pending["state"] == "AmendmentPending" and next_pending["version"] == 6
    record_filing(incident_id, {
        **original_body, "form_type": next_pending["required_form"], "version": 6,
        "accession_number": "0001234567-26-000003", "unavailable_elements": [],
    }, **actor)
    rejected(incident_id, actor, {**AMENDMENT, "elements": ["nature"]},
             ErrorCode.VALIDATION_ERROR, "elements[0]")
    before = get_incident(incident_id, **actor)
    closed = workflow.transition_incident(incident_id, {"to": "Closed", "version": 7}, **actor)
    assert closed == {**before, "state": "Closed", "version": 8}


@pytest.mark.parametrize("state", [State.DISCLOSURE_RECORDED, State.CLOSED])
def test_unsupported_amendment_deadline_is_validation_error_before_state(state):
    incident_id, actor, _ = recorded_case(
        test_now=datetime(MAX_CALENDAR_YEAR + 1, 1, 10, tzinfo=UTC)
    )
    set_state(incident_id, state)
    rejected(incident_id, actor, {
        **AMENDMENT, "info_available_date": f"{MAX_CALENDAR_YEAR}-12-31",
    }, ErrorCode.VALIDATION_ERROR, "info_available_date")


@pytest.mark.parametrize("who", [RESPONDER, BOARD])
def test_amendment_requires_approver(who):
    incident_id, actor, _ = recorded_case()
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        workflow.transition_incident(incident_id, AMENDMENT, **in_session(who, actor["session_id"]))
    assert error.value.code == ErrorCode.FORBIDDEN_ROLE
    assert table_counts() == counts


def test_amendment_is_scoped_to_session():
    incident_id, _, _ = recorded_case()
    other_actor = in_session(APPROVER, new_session())
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        workflow.transition_incident(incident_id, AMENDMENT, **other_actor)
    assert error.value.code == ErrorCode.NOT_FOUND
    assert table_counts() == counts


@pytest.mark.parametrize("available,due", [
    ("2026-10-03", "2026-10-08"),
    ("2026-10-09", "2026-10-16"),  # Observed federal holiday on Monday October 12.
    ("2026-12-24", "2026-12-31"),
])
def test_amendment_deadline_counts_from_availability_excluding_holidays(available, due):
    incident_id, actor, _ = recorded_case(test_now=datetime(2027, 1, 5, tzinfo=UTC))
    result = workflow.transition_incident(incident_id, {
        **AMENDMENT, "info_available_date": available,
    }, **actor)
    assert result["due_date"] == due


def test_audit_failure_rolls_back_form_deadline_state_and_version(monkeypatch):
    incident_id, actor, _ = recorded_case()
    before, counts = get_incident(incident_id, **actor), table_counts()
    real_audit = workflow.audit

    def fail_after_insert(*args, **kwargs):
        real_audit(*args, **kwargs)
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(workflow, "audit", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        workflow.transition_incident(incident_id, AMENDMENT, **actor)
    assert get_incident(incident_id, **actor) == before
    assert table_counts() == counts
