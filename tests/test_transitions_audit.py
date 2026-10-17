"""Phase 3 transitions and audit reads against the real local PostgreSQL database."""

import pytest

from app import clock
from app.errors import ErrorCode, TracerError
from app.models import State
from app.services import workflow
from app.services.evidence import import_evidence
from app.services.incidents import get_incident
from tests.helpers import APPROVER, BOARD, RESPONDER, audit_entries, set_state, table_counts, ticket

PHASE3_STATES = [State.UNDER_ASSESSMENT, State.DETERMINED_MATERIAL,
                 State.DETERMINED_NOT_MATERIAL, State.DISCLOSURE_DELAYED, State.CLOSED]
ALLOWED = {(State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
           (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
           (State.DETERMINED_NOT_MATERIAL, State.CLOSED)}


def assert_rejected(incident_id, body, code, field=None, who=APPROVER):
    counts = table_counts()
    previous = get_incident(incident_id, **BOARD) if incident_id != "missing" else None
    with pytest.raises(TracerError) as error:
        workflow.transition_incident(incident_id, body, **who)
    assert (error.value.code, error.value.field) == (code, field)
    assert table_counts() == counts
    if previous is not None:
        assert get_incident(incident_id, **BOARD) == previous


@pytest.mark.parametrize("source,target", [(source, target) for source in PHASE3_STATES for target in PHASE3_STATES])
def test_phase3_transition_pairs(incident, source, target):
    ident = incident["incident_id"]
    set_state(ident, source)
    if (source, target) not in ALLOWED:
        assert_rejected(ident, {"to": target.value, "version": 1}, ErrorCode.INVALID_TRANSITION)
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
    assert start <= entry.timestamp <= clock.utcnow()
    assert entry.before == {"state": source.value, "version": 1}
    assert entry.after == {"state": target.value, "version": 2}


@pytest.mark.parametrize("body,field", [(None, None), ([], None), ({}, "to"),
    ({"to": 1, "version": 1}, "to"), ({"to": "unknown", "version": 1}, "to"),
    *[({"to": "Closed", "version": version}, "version") for version in [None, True, False, 0, -1, 1.5, "1"]]])
def test_body_before_state(incident, body, field):
    assert_rejected(incident["incident_id"], body, ErrorCode.VALIDATION_ERROR, field)


@pytest.mark.parametrize("who", [RESPONDER, BOARD])
def test_role_before_existence_and_body(who):
    assert_rejected("missing", None, ErrorCode.FORBIDDEN_ROLE, who=who)


def test_existence_before_body():
    assert_rejected("missing", None, ErrorCode.NOT_FOUND)


@pytest.mark.parametrize("version", [1, 1.0])
def test_stale_version_and_existing_determination(incident, version):
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
    ident = incident["incident_id"]
    set_state(ident, State.DETERMINED_NOT_MATERIAL)
    def fail(tx, *args, **kwargs):
        tx.flush()  # The state update has reached PostgreSQL before the simulated failure.
        raise TracerError(ErrorCode.NOT_IMPLEMENTED, "Simulated audit failure")
    monkeypatch.setattr(workflow, "audit", fail)
    assert_rejected(ident, {"to": "Closed", "version": 1}, ErrorCode.NOT_IMPLEMENTED)


@pytest.mark.parametrize("who", [RESPONDER, APPROVER, BOARD])
def test_audit_read_is_scoped_ordered_and_does_not_write(incident, who):
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


def test_unknown_audit_incident_does_not_write():
    counts = table_counts()
    with pytest.raises(TracerError) as error:
        workflow.list_audit_entries("missing", **BOARD)
    assert (error.value.code, error.value.field) == (ErrorCode.NOT_FOUND, None)
    assert table_counts() == counts
