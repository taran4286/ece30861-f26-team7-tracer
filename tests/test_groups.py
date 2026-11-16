"""app/services/groups.py: creating groups (REQ-04, AC-04), and determinations on grouped incidents."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from app.errors import ErrorCode, TracerError
from app.models import AuditAction, State
from app.services import control, workflow
from app.services.evidence import import_evidence
from app.services.groups import create_group
from app.services.incidents import create_incident, get_incident
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    audit_entries,
    in_session,
    new_session,
    set_state,
    table_counts,
    ticket,
)

pytestmark = pytest.mark.usefixtures("default_session")


def new_incident(discovery_date: str = "2026-10-01", who: dict[str, Any] = RESPONDER) -> str:
    return create_incident({"title": "Ransomware", "discovery_date": discovery_date}, **who)["incident_id"]


def determination_body(incident_id: str, version: int, **overrides: Any) -> dict[str, Any]:
    """A Material determination citing one new evidence item of the incident."""
    [item] = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]},
                             **RESPONDER)["evidence"]
    body = {"decision": "Material", "determination_date": "2026-10-05",
            "rationale": "Customer data exposed", "evidence_refs": [item["evidence_id"]],
            "version": version}
    return {**body, **overrides}


def rejected(call, code, field=None, incident_ids=()):
    """Run a call that must fail, and check that nothing was stored or changed."""
    before = table_counts()
    previous = [get_incident(incident_id, **BOARD) for incident_id in incident_ids]
    with pytest.raises(TracerError) as error:
        call()
    assert (error.value.code, error.value.field) == (code, field)
    assert table_counts() == before
    assert [get_incident(incident_id, **BOARD) for incident_id in incident_ids] == previous


# --- Creating a group --------------------------------------------------------------------------


@pytest.mark.parametrize("who", [RESPONDER, APPROVER])
def test_a_group_links_its_members(who):
    ids = [new_incident() for _ in range(3)]
    group = create_group({"incident_ids": ids}, **who)

    assert group["group_id"].startswith("grp_")
    # Members come back by incident_id, as the snapshot lists them.
    assert group["incident_ids"] == sorted(ids)
    for incident_id in ids:
        incident = get_incident(incident_id, **BOARD)
        assert (incident["group_id"], incident["version"], incident["state"]) == (
            group["group_id"], 2, "UnderAssessment")
        [created, linked] = audit_entries(incident_id)
        assert created.action == AuditAction.CREATE_INCIDENT
        assert linked.action == AuditAction.CREATE_GROUP
        assert linked.user_id == who["user_id"]
        assert linked.before == {"group_id": None, "version": 1}
        assert linked.after == {"group_id": group["group_id"], "incident_ids": sorted(ids),
                                "version": 2}


def test_the_snapshot_lists_the_sessions_groups():
    session_id = new_session()
    responder = in_session(RESPONDER, session_id)
    first = create_group({"incident_ids": [new_incident(who=responder) for _ in range(2)]},
                         **responder)
    second = create_group({"incident_ids": [new_incident(who=responder) for _ in range(3)]},
                          **responder)
    # Another session's group stays out.
    create_group({"incident_ids": [new_incident(), new_incident()]}, **RESPONDER)

    groups = control.snapshot(session_id)["groups"]
    assert groups == sorted([first, second], key=lambda group: group["group_id"])


def test_board_cannot_create_a_group():
    ids = [new_incident(), new_incident()]
    rejected(lambda: create_group({"incident_ids": ids}, **BOARD), ErrorCode.FORBIDDEN_ROLE,
             incident_ids=ids)


def test_the_session_comes_before_the_role_and_the_role_before_the_body():
    rejected(lambda: create_group(None, **in_session(BOARD, "pytest-missing")),
             ErrorCode.NOT_FOUND)
    rejected(lambda: create_group(None, **BOARD), ErrorCode.FORBIDDEN_ROLE)


@pytest.mark.parametrize("body,field", [
    (None, None),
    ([], None),
    ({}, "incident_ids"),
    ({"incident_ids": "inc_1"}, "incident_ids"),
    ({"incident_ids": []}, "incident_ids"),
    # AC-04: a group needs two incidents, so an incident can't be linked to itself.
    ({"incident_ids": ["MEMBER"]}, "incident_ids"),
    ({"incident_ids": ["MEMBER", "MEMBER"]}, "incident_ids[1]"),
    ({"incident_ids": ["MEMBER", 5]}, "incident_ids[1]"),
    ({"incident_ids": ["MEMBER", None]}, "incident_ids[1]"),
    ({"incident_ids": ["MEMBER", " "]}, "incident_ids[1]"),
    ({"incident_ids": ["MEMBER", "inc\x00x"]}, "incident_ids[1]"),
    # Every entry's shape is checked before any ID is looked up.
    ({"incident_ids": ["inc_missing", 5]}, "incident_ids[1]"),
])
def test_an_invalid_body_is_rejected(body, field):
    member = new_incident()
    if isinstance(body, dict) and isinstance(body.get("incident_ids"), list):
        body = {"incident_ids": [member if i == "MEMBER" else i for i in body["incident_ids"]]}
    rejected(lambda: create_group(body, **RESPONDER), ErrorCode.VALIDATION_ERROR, field,
             incident_ids=[member])


@pytest.mark.parametrize("ids,field", [
    (["MEMBER", "inc_missing"], "incident_ids[1]"),
    (["inc_missing", "MEMBER"], "incident_ids[0]"),
    (["inc_missing", "inc_other"], "incident_ids[0]"),
])
def test_an_unknown_incident_is_not_found(ids, field):
    member = new_incident()
    ids = [member if i == "MEMBER" else i for i in ids]
    rejected(lambda: create_group({"incident_ids": ids}, **RESPONDER), ErrorCode.NOT_FOUND,
             field, incident_ids=[member])


def test_another_sessions_incident_is_unknown():
    mine = new_incident()
    theirs = new_incident(who=in_session(RESPONDER, new_session()))
    rejected(lambda: create_group({"incident_ids": [mine, theirs]}, **RESPONDER),
             ErrorCode.NOT_FOUND, "incident_ids[1]", incident_ids=[mine])


def test_linking_grouped_incidents_again_changes_nothing():
    """AC-04's duplicate link: a member already in a group is a 409, and nothing changes."""
    ids = [new_incident(), new_incident()]
    create_group({"incident_ids": ids}, **RESPONDER)
    rejected(lambda: create_group({"incident_ids": ids}, **RESPONDER), ErrorCode.CONFLICT,
             incident_ids=ids)
    fresh = new_incident()
    rejected(lambda: create_group({"incident_ids": [fresh, ids[0]]}, **RESPONDER),
             ErrorCode.CONFLICT, incident_ids=[fresh, *ids])


@pytest.mark.parametrize("state", [s for s in State if s != State.UNDER_ASSESSMENT])
def test_only_under_assessment_incidents_can_be_grouped(state):
    ids = [new_incident(), new_incident()]
    set_state(ids[1], state)
    rejected(lambda: create_group({"incident_ids": ids}, **RESPONDER), ErrorCode.CONFLICT,
             incident_ids=ids)


def test_an_unknown_incident_comes_before_the_conflict():
    grouped = [new_incident(), new_incident()]
    create_group({"incident_ids": grouped}, **RESPONDER)
    rejected(lambda: create_group({"incident_ids": [grouped[0], "inc_missing"]}, **RESPONDER),
             ErrorCode.NOT_FOUND, "incident_ids[1]", incident_ids=grouped)


def test_competing_groups_over_one_incident_link_it_once():
    shared, left, right = new_incident(), new_incident(), new_incident()
    ready = Barrier(2)

    def submit(other):
        ready.wait(timeout=10)
        try:
            return create_group({"incident_ids": [other, shared]}, **RESPONDER)["group_id"]
        except TracerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [left, right]))
    assert results.count(ErrorCode.CONFLICT) == 1
    [group_id] = [result for result in results if result != ErrorCode.CONFLICT]
    assert get_incident(shared, **BOARD)["group_id"] == group_id
    assert get_incident(shared, **BOARD)["version"] == 2


# --- Determinations on a group ---------------------------------------------------------------


@pytest.mark.parametrize("decision,state,form,due", [
    ("Material", "DeterminedMaterial", "8-K Item 1.05", "2026-10-09"),
    ("NotMaterial", "DeterminedNotMaterial", None, None),
])
def test_a_determination_applies_to_every_member(decision, state, form, due):
    """AC-04: the group's determination shows on all of its members."""
    ids = [new_incident() for _ in range(3)]
    create_group({"incident_ids": ids}, **RESPONDER)
    body = determination_body(ids[1], 2, decision=decision)

    result = workflow.record_determination(ids[1], body, **APPROVER)

    assert result == get_incident(ids[1], **BOARD)
    for incident_id in ids:
        incident = get_incident(incident_id, **BOARD)
        assert (incident["state"], incident["required_form"], incident["due_date"],
                incident["version"]) == (state, form, due, 3)
        determination = incident["determination"]
        assert determination == result["determination"]
        assert determination["evidence_refs"] == body["evidence_refs"]
        entry = audit_entries(incident_id)[-1]
        assert entry.action == AuditAction.RECORD_DETERMINATION
        assert entry.before == {"state": "UnderAssessment", "determination": None, "version": 2}
        assert entry.after["version"] == 3


def test_an_ungrouped_incident_is_determined_alone():
    ids = [new_incident(), new_incident()]
    workflow.record_determination(ids[0], determination_body(ids[0], 1), **APPROVER)
    assert get_incident(ids[1], **BOARD)["state"] == "UnderAssessment"


def test_no_member_is_determined_before_its_discovery():
    ids = [new_incident("2026-10-01"), new_incident("2026-10-04")]
    create_group({"incident_ids": ids}, **RESPONDER)
    body = determination_body(ids[0], 2, determination_date="2026-10-03")
    rejected(lambda: workflow.record_determination(ids[0], body, **APPROVER),
             ErrorCode.VALIDATION_ERROR, "determination_date", incident_ids=ids)
    # The latest discovery date itself is allowed.
    workflow.record_determination(ids[0], {**body, "determination_date": "2026-10-04"}, **APPROVER)
    assert get_incident(ids[1], **BOARD)["determination"]["determination_date"] == "2026-10-04"


def test_a_stale_group_determination_changes_no_member():
    ids = [new_incident(), new_incident()]
    create_group({"incident_ids": ids}, **RESPONDER)
    # Version 1 was read before the group was created.
    body = determination_body(ids[0], 1)
    rejected(lambda: workflow.record_determination(ids[0], body, **APPROVER),
             ErrorCode.STALE_VERSION, incident_ids=ids)


def test_competing_determinations_on_one_group_save_once():
    """Members are locked in one order, so two requests on different members don't deadlock:
    the second waits, then sees the first one's version."""
    ids = [new_incident(), new_incident()]
    create_group({"incident_ids": ids}, **RESPONDER)
    bodies = {incident_id: determination_body(incident_id, 2) for incident_id in ids}
    ready = Barrier(2)

    def submit(incident_id):
        ready.wait(timeout=10)
        try:
            workflow.record_determination(incident_id, bodies[incident_id], **APPROVER)
            return "saved"
        except TracerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ids))
    assert sorted(results, key=str) == sorted(["saved", ErrorCode.STALE_VERSION], key=str)
    for incident_id in ids:
        incident = get_incident(incident_id, **BOARD)
        assert (incident["state"], incident["version"]) == ("DeterminedMaterial", 3)
        actions = [entry.action for entry in audit_entries(incident_id)]
        assert actions.count(AuditAction.RECORD_DETERMINATION) == 1
