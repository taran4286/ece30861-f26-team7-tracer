"""Draft preparation and validation with fixture evidence and fake model output."""

import pytest

from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import FormType, Incident, State
from app.roles import Role
from app.services.drafts import load_draft_context, validate_draft_elements
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from app.services.workflow import record_determination
from tests.helpers import APPROVER, BOARD, RESPONDER, new_session, set_state, table_counts, ticket


def draft_elements(*citations):
    return {
        "nature": {"text": "A ticket recorded ransomware.", "citations": list(citations),
                   "sensitive_flags": []},
        **{
            name: {"text": "Not yet determined", "citations": [], "sensitive_flags": []}
            for name in ("scope", "timing", "impact")
        },
    }


@pytest.mark.parametrize("elements,field", [
    ({"nature": {}}, "elements"),
    (draft_elements(), "elements.nature.citations"),
    (draft_elements("ev_ours", "ev_other"), "elements.nature.citations[1]"),
    (draft_elements(["ev_ours"]), "elements.nature.citations[0]"),
    ({**draft_elements("ev_ours"), "impact": {
        "text": "Not yet determined", "citations": ["ev_ours"], "sensitive_flags": [],
    }}, "elements.impact"),
    ({**draft_elements("ev_ours"), "materiality": "Material"}, "elements"),
])
def test_rejects_invalid_model_output(elements, field):
    with pytest.raises(TracerError) as raised:
        validate_draft_elements(elements, evidence_ids={"ev_ours"})
    assert (raised.value.code, raised.value.field) == (ErrorCode.VALIDATION_ERROR, field)


def test_accepts_a_draft_with_no_evidence_when_all_elements_are_undetermined():
    elements = draft_elements()
    elements["nature"]["text"] = "Not yet determined"
    validate_draft_elements(elements, evidence_ids=set())


def test_accepts_drafted_text_with_incident_evidence():
    elements = draft_elements("ev_first", "ev_second")
    validate_draft_elements(elements, evidence_ids={"ev_first", "ev_second"})


@pytest.mark.parametrize("state,form", [
    (State.DETERMINED_MATERIAL, FormType.FORM_8K_ITEM_105),
    (State.DISCLOSURE_DELAYED, FormType.FORM_6K),
    (State.AMENDMENT_PENDING, FormType.FORM_8KA),
])
def test_eligible_drafts_use_scoped_evidence_without_changing_workflow(incident, state, form):
    incident_id = incident["incident_id"]
    other = create_incident({"title": "Other", "discovery_date": "2026-10-01"}, **RESPONDER)
    evidence_id = import_evidence(
        incident_id, {"source": "ticketing", "items": [ticket()]}, **RESPONDER,
    )["evidence"][0]["evidence_id"]
    other_id = import_evidence(
        other["incident_id"], {"source": "ticketing", "items": [ticket(2)]}, **RESPONDER,
    )["evidence"][0]["evidence_id"]
    record_determination(incident_id, {
        "decision": "Material", "determination_date": "2026-10-05",
        "rationale": "Fixture evidence", "evidence_refs": [evidence_id], "version": 1,
    }, **APPROVER)
    with transaction() as tx:
        stored = tx.get(Incident, incident_id)
        stored.state = state
        stored.required_form = form

    before = get_incident(incident_id, **BOARD)
    counts = table_counts()
    context = load_draft_context(incident_id, role=APPROVER["role"], session_id=APPROVER["session_id"])
    assert context["form_type"] == form.value
    assert context["llm_base_url"] == "https://llm.invalid/v1"
    evidence_ids = {item["evidence_id"] for item in context["evidence"]}
    assert evidence_ids == {evidence_id}
    validate_draft_elements(draft_elements(evidence_id), evidence_ids=evidence_ids)
    with pytest.raises(TracerError) as raised:
        validate_draft_elements(draft_elements(other_id), evidence_ids=evidence_ids)
    assert raised.value.field == "elements.nature.citations[0]"
    assert get_incident(incident_id, **BOARD) == before
    assert table_counts() == counts


@pytest.mark.parametrize("state,code", [
    (State.UNDER_ASSESSMENT, ErrorCode.NOT_MATERIAL),
    (State.DETERMINED_NOT_MATERIAL, ErrorCode.NOT_MATERIAL),
    (State.DISCLOSURE_RECORDED, ErrorCode.INVALID_TRANSITION),
    (State.CLOSED, ErrorCode.INVALID_TRANSITION),
])
def test_ineligible_states_reject_drafting(incident, state, code):
    incident_id = incident["incident_id"]
    set_state(incident_id, state)
    before = get_incident(incident_id, **BOARD)
    counts = table_counts()
    with pytest.raises(TracerError) as raised:
        load_draft_context(incident_id, role=APPROVER["role"], session_id=APPROVER["session_id"])
    assert (raised.value.status, raised.value.code) == (409, code)
    assert get_incident(incident_id, **BOARD) == before
    assert table_counts() == counts


def test_draft_checks_session_before_role_and_role_before_incident(incident):
    with pytest.raises(TracerError) as raised:
        load_draft_context("inc_missing", role=Role.BOARD, session_id="missing-session")
    assert raised.value.code == ErrorCode.NOT_FOUND

    for role in (Role.BOARD, Role.RESPONDER):
        with pytest.raises(TracerError) as raised:
            load_draft_context("inc_missing", role=role, session_id=APPROVER["session_id"])
        assert raised.value.code == ErrorCode.FORBIDDEN_ROLE

    with pytest.raises(TracerError) as raised:
        load_draft_context(incident["incident_id"], role=Role.DISCLOSURE_APPROVER,
                           session_id=new_session())
    assert raised.value.code == ErrorCode.NOT_FOUND
