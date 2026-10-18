"""Determination behavior against real PostgreSQL, plus the AC-09 source guardrail."""

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import clock, db
from app.errors import ErrorCode, TracerError
from app.models import Determination, Incident, RegistrantType, State
from app.services import workflow
from app.services.evidence import import_evidence
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
    set_state,
    table_counts,
    ticket,
)

pytestmark = pytest.mark.usefixtures("default_session")


def new_case(incident, responder=RESPONDER):
    """Two evidence items on the incident, and a complete Material determination body citing them."""
    evidence = import_evidence(incident['incident_id'], {
        'source': 'ticketing', 'items': [ticket(1), ticket(2)]
    }, **responder)['evidence']
    return incident['incident_id'], {
        'decision': 'Material', 'determination_date': incident['discovery_date'],
        'rationale': 'Significant impact', 'evidence_refs': [e['evidence_id'] for e in evidence],
        'version': 1,
    }


@pytest.fixture
def case(incident):
    return new_case(incident)


def rejected(case, code=ErrorCode.VALIDATION_ERROR, field=None, who=APPROVER):
    incident_id, body = case
    before = table_counts()
    # Counts alone cannot detect changes to existing rows.
    previous = get_incident(incident_id, **BOARD) if incident_id != 'missing' else None
    with pytest.raises(TracerError) as error:
        workflow.record_determination(incident_id, body, **who)
    assert (error.value.code, error.value.field) == (code, field)
    assert table_counts() == before
    if previous is not None:
        assert get_incident(incident_id, **BOARD) == previous


@pytest.mark.parametrize('registrant,decision,state,form', [
    ('Domestic', 'Material', 'DeterminedMaterial', '8-K Item 1.05'),
    ('FPI', 'Material', 'DeterminedMaterial', '6-K'),
    ('Domestic', 'NotMaterial', 'DeterminedNotMaterial', None),
    ('FPI', 'NotMaterial', 'DeterminedNotMaterial', None),
])
def test_success(registrant, decision, state, form):
    # The registrant type is fixed per session, so each case gets its own.
    session_id = new_session(registrant_type=RegistrantType(registrant))
    responder, approver, board = (in_session(who, session_id) for who in (RESPONDER, APPROVER, BOARD))
    incident = create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"}, **responder)
    incident_id, body = new_case(incident, responder)
    body['decision'] = decision
    start = clock.utcnow()
    result = workflow.record_determination(incident_id, body, **approver)
    assert result == get_incident(incident_id, **board)
    assert (result['state'], result['required_form'], result['version']) == (state, form, 2)
    assert result['due_date'] is None and result['flags'] == []
    with db.transaction() as tx:
        stored = tx.get(Determination, incident_id)
        assert stored.decision.value == decision
        assert stored.determination_date.isoformat() == body['determination_date']
        assert stored.rationale == body['rationale'] and stored.evidence_refs == body['evidence_refs']
        assert stored.voluntary_disclosure is False and stored.recorded_by == APPROVER['user_id']
        assert start <= stored.recorded_at <= clock.utcnow()
    entries = audit_entries(incident_id)
    assert len(entries) == 3
    entry = entries[-1]
    assert entry.action.value == 'record_determination' and entry.user_id == APPROVER['user_id']
    assert entry.session_id == session_id
    assert entry.before == {'state': 'UnderAssessment', 'determination': None, 'version': 1}
    assert entry.after == {'state': state, 'required_form': form, 'version': 2,
                           'determination': {'decision': decision, 'determination_date': body['determination_date']}}


@pytest.mark.parametrize('who', [RESPONDER, BOARD])
def test_role_before_incident_and_body(who):
    rejected(('missing', {}), ErrorCode.FORBIDDEN_ROLE, who=who)


def test_unknown_incident_before_body():
    rejected(('missing', {}), ErrorCode.NOT_FOUND)


@pytest.mark.parametrize('field', ['decision', 'determination_date', 'rationale', 'evidence_refs', 'version'])
def test_missing_field(case, field):
    del case[1][field]
    rejected(case, field=field)


@pytest.mark.parametrize('field,value', [
    ('decision', 'unknown'), ('decision', []), ('rationale', ''), ('rationale', None),
    ('rationale', 'contains\x00nul'), ('evidence_refs', []), ('evidence_refs', ['missing']),
    ('evidence_refs', ['nul\x00']), ('evidence_refs', [{}]),
    ('determination_date', '2026-02-30'), ('determination_date', '20261001'),
    ('version', 0), ('version', True), ('version', 1.5), ('version', '1'),
    ('voluntary_disclosure', None), ('voluntary_disclosure', 'yes'), ('voluntary_disclosure', 0),
])
def test_invalid_field(case, field, value):
    case[1][field] = value
    rejected(case, field=field)


@pytest.mark.parametrize('body', [None, [], 'invalid'])
def test_non_object_body(case, body):
    rejected((case[0], body))


def test_first_bad_field_before_later_missing_field(case):
    case[1]['decision'] = 'invalid'
    del case[1]['version']
    rejected(case, field='decision')


def test_duplicate_refs(case):
    case[1]['evidence_refs'] *= 2
    rejected(case, field='evidence_refs')


def test_other_incident_ref(case):
    incident = create_incident({"title": "Other", "discovery_date": "2026-10-01"}, **RESPONDER)
    evidence = import_evidence(incident['incident_id'], {'source': 'ticketing', 'items': [ticket()]}, **RESPONDER)
    case[1]['evidence_refs'] = [evidence['evidence'][0]['evidence_id']]
    rejected(case, field='evidence_refs')


@pytest.mark.parametrize('offset', [-1, 1])
def test_out_of_range_dates(case, offset):
    day = date.fromisoformat(case[1]['determination_date']) if offset < 0 else TODAY
    case[1]['determination_date'] = (day + timedelta(days=offset)).isoformat()
    rejected(case, field='determination_date')


@pytest.mark.parametrize('boundary', ['discovery', 'today'])
def test_date_boundaries(case, boundary):
    if boundary == 'today':
        case[1]['determination_date'] = TODAY.isoformat()
    assert workflow.record_determination(case[0], case[1], **APPROVER)['version'] == 2


@pytest.mark.parametrize('state', [s for s in State if s != State.UNDER_ASSESSMENT])
def test_wrong_state_after_validation(case, state):
    set_state(case[0], state)
    case[1]['decision'] = 'bad'
    rejected(case, field='decision')
    case[1]['decision'] = 'Material'
    rejected(case, ErrorCode.INVALID_TRANSITION)


def test_second_determination(case):
    workflow.record_determination(case[0], case[1], **APPROVER)
    rejected(case, ErrorCode.INVALID_TRANSITION)


@pytest.mark.parametrize('value', [True, False])
def test_voluntary_disclosure_boolean(case, value):
    case[1]['voluntary_disclosure'] = value
    rejected(case, ErrorCode.NOT_IMPLEMENTED)


@pytest.mark.parametrize('version', [1, 1.0])
def test_stale_and_integral_float_versions(case, version):
    with db.transaction() as tx:
        tx.get(Incident, case[0]).version = 5
    case[1]['version'] = version
    assert workflow.record_determination(case[0], case[1], **APPROVER)['version'] == 6


def test_audit_failure_rolls_back(case, monkeypatch):
    def fail(tx, *args, **kwargs):
        tx.flush()
        raise TracerError(ErrorCode.NOT_IMPLEMENTED, 'Simulated audit failure')
    monkeypatch.setattr(workflow, 'audit', fail)
    rejected(case, ErrorCode.NOT_IMPLEMENTED)


def materiality_writes(source):
    """Detect constructors, ORM/bulk writes, direct fields and literal SQL writes.

    A source guardrail for repository conventions, not arbitrary dynamic Python.
    """
    import re
    tree = ast.parse(source)
    aliases = {a.asname or a.name: a.name for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) for a in n.names}
    def name(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        return node.attr if isinstance(node, ast.Attribute) else None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = name(node.func)
            if called == 'Determination' or (
                called in {'insert', 'update', 'delete', 'bulk_insert_mappings', 'bulk_update_mappings'}
                and any(name(part) in {'Determination', 'determinations'}
                        for target in [node.func, *node.args] for part in ast.walk(target))
            ):
                yield node.lineno
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and node.attr in {'decision', 'determination'}:
            yield node.lineno
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(
            r'\b(?:insert\s+into|update|delete\s+from)\s+(?:public\.)?["\x27]?determinations\b', node.value, re.I
        ):
            yield node.lineno


def test_no_other_service_writes_materiality():
    root = Path(__file__).resolve().parents[1] / 'app' / 'services'
    for path in root.rglob('*.py'):
        if path.name != 'workflow.py':
            assert not list(materiality_writes(path.read_text())), str(path)


@pytest.mark.parametrize('statement', [
    'Determination(decision="Material")', 'incident.determination = value',
    'determination.decision = value', 'tx.execute(update(Determination))',
    'tx.bulk_insert_mappings(Determination, rows)',
    'Determination.__table__.insert()', 'tx.query(Determination).update(values)', 'tx.execute(text("UPDATE determinations SET decision = 1"))',
])
def test_guardrail_detects_writes(statement):
    assert list(materiality_writes(statement))


def test_nul_title_rejected():
    before = table_counts()
    with pytest.raises(TracerError) as error:
        create_incident({'title': 'bad\x00title', 'discovery_date': '2026-10-01'}, **RESPONDER)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, 'title')
    assert table_counts() == before


def test_nul_import_summary_rejected(case):
    before = table_counts()
    previous = get_incident(case[0], **BOARD)
    with pytest.raises(TracerError) as error:
        import_evidence(case[0], {'source': 'ticketing', 'items': [ticket(summary='bad\x00summary')]}, **RESPONDER)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, 'items[0].summary')
    assert table_counts() == before
    assert get_incident(case[0], **BOARD) == previous


PHASE3_STATES = [State.UNDER_ASSESSMENT, State.DETERMINED_MATERIAL,
                 State.DETERMINED_NOT_MATERIAL, State.DISCLOSURE_DELAYED, State.CLOSED]
ALLOWED = {(State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
           (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
           (State.DETERMINED_NOT_MATERIAL, State.CLOSED)}


def assert_rejected(incident_id, body, code, field=None, who=APPROVER):
    """Check a rejected transition returns the expected error and changes nothing."""
    counts = table_counts()
    previous = get_incident(incident_id, **BOARD) if incident_id != "missing" else None
    with pytest.raises(TracerError) as error:
        workflow.transition_incident(incident_id, body, **who)
    assert (error.value.code, error.value.field) == (code, field)
    assert table_counts() == counts
    if previous is not None:
        assert get_incident(incident_id, **BOARD) == previous


@pytest.mark.parametrize("source,target", [(source, target) for source in PHASE3_STATES for target in list(State)])
def test_phase3_transition_pairs(incident, source, target):
    """Check allowed state changes succeed and other Phase 3 pairs are rejected."""
    ident = incident["incident_id"]
    set_state(ident, source)
    body = {"to": target.value, "version": 1}
    if target == State.AMENDMENT_PENDING:
        body.update(info_available_date=incident["discovery_date"], elements=["nature"])
    if (source, target) not in ALLOWED:
        assert_rejected(ident, body, ErrorCode.INVALID_TRANSITION)
        return
    before = get_incident(ident, **BOARD)
    entries = audit_entries(ident)
    start = clock.utcnow()
    result = workflow.transition_incident(ident, {"to": target.value, "version": 1}, **APPROVER)
    assert result == {**before, "state": target.value, "version": before["version"] + 1}
    assert result == get_incident(ident, **BOARD)
    saved = audit_entries(ident)
    assert len(saved) == len(entries) + 1
    entry = saved[-1]
    assert entry.action.value == "transition" and entry.user_id == APPROVER["user_id"]
    assert entry.session_id == SESSION_ID
    assert start <= entry.timestamp <= clock.utcnow()
    assert entry.before == {"state": source.value, "version": 1}
    assert entry.after == {"state": target.value, "version": 2}


@pytest.mark.parametrize("body,field", [(None, None), ([], None), ({}, "to"),
    ({"to": 1, "version": 1}, "to"), ({"to": "unknown", "version": 1}, "to"),
    *[({"to": "Closed", "version": version}, "version") for version in [None, True, False, 0, -1, 1.5, "1"]]])
def test_body_before_state(incident, body, field):
    """Check invalid request fields are reported before a disallowed state change."""
    assert_rejected(incident["incident_id"], body, ErrorCode.VALIDATION_ERROR, field)


@pytest.mark.parametrize("who", [RESPONDER, BOARD])
def test_role_before_existence_and_body(who):
    """Check permission errors come before incident and body errors."""
    assert_rejected("missing", None, ErrorCode.FORBIDDEN_ROLE, who=who)


def test_existence_before_body():
    """Check an unknown incident is reported before an invalid body."""
    assert_rejected("missing", None, ErrorCode.NOT_FOUND)


@pytest.mark.parametrize("version", [1, 1.0])
def test_stale_version_and_existing_determination(incident, version):
    """Check transitions accept stale whole-number versions and preserve determination data."""
    ident = incident["incident_id"]
    evidence = import_evidence(ident, {"source": "ticketing", "items": [ticket()]}, **RESPONDER)
    determined = workflow.record_determination(ident, {
        "decision": "Material", "determination_date": incident["discovery_date"],
        "rationale": "Significant impact", "evidence_refs": [evidence["evidence"][0]["evidence_id"]],
        "version": 1,
    }, **APPROVER)
    for target in ["DisclosureDelayed", "DeterminedMaterial"]:
        result = workflow.transition_incident(ident, {"to": target, "version": version}, **APPROVER)
        assert result["determination"] == determined["determination"]
        assert result["required_form"] == determined["required_form"]
        assert result["evidence"] == determined["evidence"]
    assert result["version"] == 4


def test_audit_failure_rolls_back_transition(incident, monkeypatch):
    """Check an audit failure also rolls back the incident update."""
    ident = incident["incident_id"]
    set_state(ident, State.DETERMINED_NOT_MATERIAL)
    def fail(tx, *args, **kwargs):
        """Simulate an audit failure after flushing the incident update."""
        tx.flush()  # The state update has reached PostgreSQL before the simulated failure.
        raise TracerError(ErrorCode.NOT_IMPLEMENTED, "Simulated audit failure")
    monkeypatch.setattr(workflow, "audit", fail)
    assert_rejected(ident, {"to": "Closed", "version": 1}, ErrorCode.NOT_IMPLEMENTED)


@pytest.mark.parametrize("who", [RESPONDER, APPROVER, BOARD])
def test_audit_read_is_scoped_ordered_and_does_not_write(incident, who):
    """Check all roles can read one incident's ordered history without changing data."""
    ident = incident["incident_id"]
    import_evidence(ident, {"source": "ticketing", "items": [ticket()]}, **RESPONDER)
    set_state(ident, State.DETERMINED_MATERIAL)
    # Create + import + eight transitions gives ten real audit entries.
    for target in ["DisclosureDelayed", "DeterminedMaterial"] * 4:
        workflow.transition_incident(ident, {"to": target, "version": 1}, **APPROVER)
    counts = table_counts()
    previous = get_incident(ident, **BOARD)
    result = workflow.list_audit_entries(ident, **who)
    stored = audit_entries(ident)
    assert len(result["entries"]) == 10
    assert [entry["entry_id"] for entry in result["entries"]] == [entry.entry_id for entry in stored]
    assert result == {"entries": [{
        "entry_id": entry.entry_id, "user_id": entry.user_id,
        "timestamp": entry.timestamp.isoformat(), "action": entry.action.value,
        "target_id": ident, "before": entry.before, "after": entry.after,
    } for entry in stored]}
    assert table_counts() == counts
    assert get_incident(ident, **BOARD) == previous


WORKFLOW_CALLS = {
    "determination": lambda ident, who: workflow.record_determination(ident, {}, **who),
    "transition": lambda ident, who: workflow.transition_incident(ident, {}, **who),
    "audit": lambda ident, who: workflow.list_audit_entries(ident, **who),
}


@pytest.mark.parametrize("operation", WORKFLOW_CALLS)
def test_unknown_session_before_role(operation):
    """Check an unknown session is reported before the role, the incident and the body."""
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        WORKFLOW_CALLS[operation]("missing", in_session(BOARD, "pytest-missing"))
    assert (error.value.code, error.value.field) == (ErrorCode.NOT_FOUND, None)
    assert table_counts() == counts


@pytest.mark.parametrize("operation", WORKFLOW_CALLS)
def test_another_sessions_incident_is_not_found(incident, operation):
    """Check an incident is invisible to every other session, before its body is checked."""
    ident = incident["incident_id"]
    other = in_session(APPROVER, new_session())
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        WORKFLOW_CALLS[operation](ident, other)
    assert (error.value.code, error.value.field) == (ErrorCode.NOT_FOUND, None)
    assert table_counts() == counts
    assert get_incident(ident, **BOARD) == incident


def test_unknown_audit_incident_does_not_write():
    """Check reading an unknown incident returns NOT_FOUND and changes nothing."""
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        workflow.list_audit_entries("missing", **BOARD)
    assert (error.value.code, error.value.field) == (ErrorCode.NOT_FOUND, None)
    assert table_counts() == counts


@pytest.mark.parametrize("value", [None, "2026-02-30", "20261001", "future"])
def test_amendment_date_validation_before_state(incident, value):
    """Reject missing, invalid and future amendment dates before checking the state."""
    if value == "future":
        value = (TODAY + timedelta(days=1)).isoformat()
    assert_rejected(incident["incident_id"], {
        "to": "AmendmentPending", "version": 1,
        "info_available_date": value, "elements": ["nature"],
    }, ErrorCode.VALIDATION_ERROR, "info_available_date")



def test_amendment_date_today_reaches_state_check(incident):
    """Accept today's date as input, then reject the Phase 4 transition."""
    assert_rejected(incident["incident_id"], {
        "to": "AmendmentPending", "version": 1,
        "info_available_date": TODAY.isoformat(), "elements": ["nature"],
    }, ErrorCode.INVALID_TRANSITION)


@pytest.mark.parametrize("elements,field", [
    (None, "elements"), ([], "elements"), ("nature", "elements"),
    (["nature", "cause"], "elements[1]"), (["Nature"], "elements[0]"), ([None], "elements[0]"),
    (["scope", "impact", "scope"], "elements[2]"),
])
def test_amendment_elements_are_unique_element_names(incident, elements, field):
    """Reject elements that aren't nature, scope, timing or impact, or that repeat one."""
    body = {"to": "AmendmentPending", "version": 1, "info_available_date": TODAY.isoformat()}
    if elements is not None:
        body["elements"] = elements
    assert_rejected(incident["incident_id"], body, ErrorCode.VALIDATION_ERROR, field)


def test_all_four_amendment_elements_reach_state_check(incident):
    """Accept every element name once, then reject the Phase 4 transition."""
    assert_rejected(incident["incident_id"], {
        "to": "AmendmentPending", "version": 1, "info_available_date": TODAY.isoformat(),
        "elements": list(workflow.DRAFT_ELEMENTS),
    }, ErrorCode.INVALID_TRANSITION)


@pytest.mark.parametrize("extra,field", [
    ({"info_available_date": "2026-02-30"}, "info_available_date"),
    ({"info_available_date": (TODAY + timedelta(days=1)).isoformat()}, "info_available_date"),
    ({"elements": ["cause"]}, "elements[0]"),
    ({"elements": []}, "elements"),
])
def test_amendment_fields_are_checked_for_every_target(incident, extra, field):
    """The request schema types the amendment fields for every target, so a bad one is a 400."""
    ident = incident["incident_id"]
    set_state(ident, State.DETERMINED_NOT_MATERIAL)
    assert_rejected(ident, {"to": "Closed", "version": 1, **extra},
                    ErrorCode.VALIDATION_ERROR, field)


def test_valid_amendment_fields_dont_block_another_target(incident):
    ident = incident["incident_id"]
    set_state(ident, State.DETERMINED_NOT_MATERIAL)
    result = workflow.transition_incident(ident, {
        "to": "Closed", "version": 1, "info_available_date": TODAY.isoformat(),
        "elements": ["impact"],
    }, **APPROVER)
    assert result["state"] == "Closed"
