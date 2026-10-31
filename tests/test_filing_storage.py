"""Filing persistence, state changes, access checks and transaction rollback."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from sqlalchemy import select

from app import clock, db
from app.errors import ErrorCode, TracerError
from app.models import Filing, FormType, Incident, RegistrantType, State
from app.services import filings
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident, incidents_to_dicts
from app.services.workflow import record_determination, transition_incident
from tests.helpers import (
    APPROVER, BOARD, RESPONDER, audit_entries, in_session, new_session,
    set_state, table_counts, ticket,
)

pytestmark = pytest.mark.usefixtures("default_session")


def material_case(registrant="Domestic", **settings):
    """Prepare a real determination and return its incident, actor and filing request."""
    session_id = new_session(registrant_type=RegistrantType(registrant), **settings)
    actor = in_session(APPROVER, session_id)
    incident = create_incident({"title": "Filing case", "discovery_date": "2026-10-01"}, **actor)
    incident_id = incident["incident_id"]
    evidence = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]}, **actor)
    result = record_determination(incident_id, {
        "decision": "Material", "determination_date": "2026-10-01",
        "rationale": "Significant impact", "version": 1,
        "evidence_refs": [evidence["evidence"][0]["evidence_id"]],
    }, **actor)
    body = {
        "form_type": result["required_form"], "filing_date": "2026-10-05",
        "accession_number": "0001234567-26-000001", "version": result["version"],
        "unavailable_elements": ["impact"],
    }
    if registrant == "FPI":
        body["home_disclosure_date"] = "2026-10-03"
    return incident_id, actor, body


@pytest.fixture
def case():
    return material_case()


def rejected(incident_id, actor, body, code, field=None):
    """A failed request changes neither stored rows nor the incident response."""
    before = get_incident(incident_id, **actor)
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        filings.record_filing(incident_id, body, **actor)
    assert (error.value.code, error.value.field) == (code, field)
    assert table_counts() == counts
    assert get_incident(incident_id, **actor) == before


@pytest.mark.parametrize("registrant", ["Domestic", "FPI"])
@pytest.mark.parametrize("delayed", [False, True])
def test_records_filing_and_audit_with_preserved_context(registrant, delayed):
    """Both registrants can file directly or during delay, with one atomic audit entry."""
    incident_id, actor, body = material_case(registrant)
    if delayed:
        transition_incident(incident_id, {"to": "DisclosureDelayed", "version": 2}, **actor)
    previous = get_incident(incident_id, **actor)
    old_entries = audit_entries(incident_id)
    started = clock.utcnow()
    result = filings.record_filing(incident_id, body, **actor)
    assert result == get_incident(incident_id, **in_session(BOARD, actor["session_id"]))
    assert result["state"] == "DisclosureRecorded"
    assert result["version"] == previous["version"] + 1
    for key in ("determination", "required_form", "due_date", "evidence", "group_id"):
        assert result[key] == previous[key]
    assert result["flags"] == []
    [filing] = result["filings"]
    assert filing == {
        "form_type": body["form_type"], "filing_date": body["filing_date"],
        "accession_number": body["accession_number"], "unavailable_elements": ["impact"],
        "home_disclosure_date": body.get("home_disclosure_date"),
        "recorded_by": actor["user_id"], "recorded_at": filing["recorded_at"],
    }
    with db.transaction() as tx:
        stored = tx.scalar(select(Filing).where(Filing.incident_id == incident_id))
        assert stored.form_type.value == body["form_type"]
        assert stored.filing_date == date(2026, 10, 5)
        assert started <= stored.recorded_at <= clock.utcnow()
        assert stored.home_disclosure_date == (date(2026, 10, 3) if registrant == "FPI" else None)
    entries = audit_entries(incident_id)
    assert len(entries) == len(old_entries) + 1
    entry = entries[-1]
    assert entry.action.value == "record_filing" and entry.user_id == actor["user_id"]
    assert entry.session_id == actor["session_id"]
    assert entry.before == {"state": previous["state"], "version": previous["version"]}
    assert entry.after == {"state": "DisclosureRecorded", "version": result["version"], "filing": filing}


def test_amendment_filing_and_oldest_first_reads(case):
    """A prepared amendment appends a filing; single and batch reads keep earlier filings."""
    incident_id, actor, body = case
    original = filings.record_filing(incident_id, body, **actor)
    # This isolates filing behavior from the separate amendment-request transition.
    with db.transaction() as tx:
        incident = tx.get(Incident, incident_id)
        incident.state = State.AMENDMENT_PENDING
        incident.required_form = FormType.FORM_8KA
        incident.due_date = date(2026, 10, 9)
    amended = filings.record_filing(incident_id, {
        **body, "form_type": "8-K/A", "accession_number": "0001234567-26-000002",
        "unavailable_elements": [],
    }, **actor)
    assert amended["state"] == "DisclosureRecorded" and amended["due_date"] == "2026-10-09"
    assert amended["filings"][0] == original["filings"][0]
    assert [item["form_type"] for item in amended["filings"]] == ["8-K Item 1.05", "8-K/A"]
    with db.transaction() as tx:
        incident = tx.get(Incident, incident_id)
        assert incidents_to_dicts(tx, [incident])[0] == amended


@pytest.mark.parametrize("state", [State.UNDER_ASSESSMENT, State.DETERMINED_NOT_MATERIAL,
                                   State.DISCLOSURE_RECORDED, State.CLOSED])
def test_disallowed_states_leave_everything_unchanged(case, state):
    incident_id, actor, body = case
    set_state(incident_id, state)
    rejected(incident_id, actor, body, ErrorCode.INVALID_TRANSITION)


@pytest.mark.parametrize("field,value", [
    ("form_type", "6-K"), ("filing_date", "2026-10-06"),
    ("filing_date", "2026-09-30"), ("accession_number", "invalid"),
    ("version", 0), ("unavailable_elements", ["other"]),
])
def test_bad_body_precedes_state_and_stores_nothing(case, field, value):
    incident_id, actor, body = case
    set_state(incident_id, State.CLOSED)
    sent = {**body, field: value}
    if field == "form_type":
        sent["home_disclosure_date"] = "2026-10-01"
    expected = "unavailable_elements[0]" if field == "unavailable_elements" else field
    rejected(incident_id, actor, sent, ErrorCode.VALIDATION_ERROR, expected)


def test_fpi_requires_home_disclosure_date():
    incident_id, actor, body = material_case("FPI")
    del body["home_disclosure_date"]
    rejected(incident_id, actor, body, ErrorCode.VALIDATION_ERROR, "home_disclosure_date")


@pytest.mark.parametrize("who", [RESPONDER, BOARD, {**APPROVER, "role": "unknown"}])
def test_role_precedes_incident_and_body(who):
    with pytest.raises(TracerError) as error:
        filings.record_filing("missing", None, **who)
    assert error.value.code == ErrorCode.FORBIDDEN_ROLE


def test_session_precedes_role_and_incident():
    with pytest.raises(TracerError) as error:
        filings.record_filing("missing", None, **in_session(BOARD, "missing"))
    assert error.value.code == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("incident_id", ["missing", "bad\x00id"])
def test_unknown_incident_precedes_body(incident_id):
    with pytest.raises(TracerError) as error:
        filings.record_filing(incident_id, None, **APPROVER)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_other_session_cannot_file_or_see_filings(case):
    incident_id, actor, body = case
    other_actor = in_session(APPROVER, new_session())
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        filings.record_filing(incident_id, body, **other_actor)
    assert error.value.code == ErrorCode.NOT_FOUND
    assert table_counts() == counts
    assert filings.record_filing(incident_id, body, **actor)["filings"]
    with pytest.raises(TracerError) as error:
        get_incident(incident_id, **other_actor)
    assert error.value.code == ErrorCode.NOT_FOUND


def test_batch_reads_keep_each_incidents_filings_separate():
    """A batch must attach each stored filing only to its own incident."""
    first_id, first_actor, first_body = material_case()
    second_id, second_actor, second_body = material_case("FPI")
    first = filings.record_filing(first_id, first_body, **first_actor)
    second = filings.record_filing(second_id, second_body, **second_actor)
    with db.transaction() as tx:
        rows = [tx.get(Incident, incident_id) for incident_id in (second_id, first_id)]
        assert incidents_to_dicts(tx, rows) == [second, first]
        assert incidents_to_dicts(tx, []) == []


def test_audit_failure_rolls_back_filing_and_incident(case, monkeypatch):
    """Even a failure after both inserts flush must undo the whole operation."""
    incident_id, actor, body = case
    before, counts = get_incident(incident_id, **actor), table_counts()
    real_audit = filings.audit

    def fail_after_insert(*args, **kwargs):
        real_audit(*args, **kwargs)
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(filings, "audit", fail_after_insert)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        filings.record_filing(incident_id, body, **actor)
    assert table_counts() == counts
    assert get_incident(incident_id, **actor) == before


def test_competing_requests_record_only_one_filing(case):
    """The row lock makes the second request see DisclosureRecorded and reject it."""
    incident_id, actor, body = case
    ready = Barrier(2)

    def submit():
        ready.wait(timeout=10)
        try:
            filings.record_filing(incident_id, body, **actor)
            return "saved"
        except TracerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert results.count("saved") == 1
    assert results.count(ErrorCode.INVALID_TRANSITION) == 1
    result = get_incident(incident_id, **actor)
    assert result["version"] == 3 and len(result["filings"]) == 1
    assert sum(entry.action.value == "record_filing" for entry in audit_entries(incident_id)) == 1
