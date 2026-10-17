"""Determination behavior against real PostgreSQL, plus the AC-09 source guardrail."""

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import clock, db
from app.config import get_settings
from app.errors import ErrorCode, TracerError
from app.models import Determination, Incident, State
from app.services import workflow
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from tests.helpers import APPROVER, BOARD, RESPONDER, audit_entries, set_state, table_counts, ticket


@pytest.fixture
def case(incident):
    evidence = import_evidence(incident['incident_id'], {
        'source': 'ticketing', 'items': [ticket(1), ticket(2)]
    }, **RESPONDER)['evidence']
    return incident['incident_id'], {
        'decision': 'Material', 'determination_date': incident['discovery_date'],
        'rationale': 'Significant impact', 'evidence_refs': [e['evidence_id'] for e in evidence],
        'version': 1,
    }


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


def clear_caches():
    db._session_factory.cache_clear()
    db.get_engine.cache_clear()
    get_settings.cache_clear()


@pytest.mark.parametrize('registrant,decision,state,form', [
    ('Domestic', 'Material', 'DeterminedMaterial', '8-K Item 1.05'),
    ('FPI', 'Material', 'DeterminedMaterial', '6-K'),
    ('Domestic', 'NotMaterial', 'DeterminedNotMaterial', None),
    ('FPI', 'NotMaterial', 'DeterminedNotMaterial', None),
])
def test_success(case, monkeypatch, registrant, decision, state, form):
    monkeypatch.setenv('REGISTRANT_TYPE', registrant)
    clear_caches()
    try:
        incident_id, body = case
        body['decision'] = decision
        start = clock.utcnow()
        result = workflow.record_determination(incident_id, body, **APPROVER)
        assert result == get_incident(incident_id, **BOARD)
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
        assert entry.before == {'state': 'UnderAssessment', 'determination': None, 'version': 1}
        assert entry.after == {'state': state, 'required_form': form, 'version': 2,
                               'determination': {'decision': decision, 'determination_date': body['determination_date']}}
    finally:
        clear_caches()


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
    day = date.fromisoformat(case[1]['determination_date']) if offset < 0 else clock.today()
    case[1]['determination_date'] = (day + timedelta(days=offset)).isoformat()
    rejected(case, field='determination_date')


@pytest.mark.parametrize('boundary', ['discovery', 'today'])
def test_date_boundaries(case, boundary):
    if boundary == 'today':
        case[1]['determination_date'] = clock.today().isoformat()
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
