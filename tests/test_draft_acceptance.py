"""app/services/drafts.py: accepting a stored draft (REQ-22)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from typing import Any

import pytest

from app.clock import utcnow
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Draft, State
from app.services.drafts import accept_draft, draft_to_dict
from app.services.incidents import create_incident, get_incident
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    add_draft,
    audit_entries,
    in_session,
    new_session,
    set_state,
    table_counts,
)

pytestmark = pytest.mark.usefixtures("default_session")


@pytest.fixture
def draft(incident) -> tuple[str, str]:
    """A Proposed draft of a DeterminedMaterial incident: (incident ID, draft ID)."""
    incident_id = incident["incident_id"]
    set_state(incident_id, State.DETERMINED_MATERIAL)
    return incident_id, add_draft(incident_id)


def stored(draft_id: str) -> dict[str, Any]:
    with transaction() as tx:
        return draft_to_dict(tx.get(Draft, draft_id))


def rejected(draft_id: str, who: dict[str, Any], code: ErrorCode, incident_id: str | None = None):
    """Accepting must fail with `code` and change nothing: no rows, the draft or the incident."""
    before = table_counts()
    previous_draft = stored(draft_id) if incident_id else None
    previous_incident = get_incident(incident_id, **BOARD) if incident_id else None
    with pytest.raises(TracerError) as error:
        accept_draft(draft_id, **who)
    assert (error.value.code, error.value.field) == (code, None)
    assert table_counts() == before
    if incident_id:
        assert stored(draft_id) == previous_draft
        assert get_incident(incident_id, **BOARD) == previous_incident


def test_accepting_marks_the_draft_accepted(draft):
    incident_id, draft_id = draft
    incident = get_incident(incident_id, **BOARD)
    proposed = stored(draft_id)
    start = utcnow()

    result = accept_draft(draft_id, **APPROVER)

    assert result == stored(draft_id)
    assert (result["status"], result["accepted_by"], result["advisory"]) == (
        "Accepted", APPROVER["user_id"], True)
    accepted_at = datetime.fromisoformat(result["accepted_at"])
    assert start <= accepted_at <= utcnow() + timedelta(seconds=1)
    # Only the acceptance fields change; the text, the form and the incident stay as they were.
    unchanged = ("draft_id", "incident_id", "form_type", "elements")
    assert {key: result[key] for key in unchanged} == {key: proposed[key] for key in unchanged}
    assert get_incident(incident_id, **BOARD) == incident

    entry = audit_entries(incident_id)[-1]
    assert (entry.action, entry.user_id, entry.target_id) == (
        AuditAction.ACCEPT_DRAFT, APPROVER["user_id"], incident_id)
    assert entry.before == {"draft_id": draft_id, "status": "Proposed", "accepted_by": None,
                            "accepted_at": None}
    assert entry.after == {"draft_id": draft_id, "status": "Accepted",
                           "accepted_by": APPROVER["user_id"], "accepted_at": result["accepted_at"]}


@pytest.mark.parametrize("who", [RESPONDER, BOARD])
def test_only_a_disclosure_approver_may_accept(draft, who):
    incident_id, draft_id = draft
    rejected(draft_id, who, ErrorCode.FORBIDDEN_ROLE, incident_id)


def test_a_draft_is_accepted_once(draft):
    incident_id, draft_id = draft
    accept_draft(draft_id, **APPROVER)
    rejected(draft_id, APPROVER, ErrorCode.CONFLICT, incident_id)


def test_the_session_comes_before_the_role_and_the_role_before_the_draft(draft):
    _, draft_id = draft
    rejected(draft_id, in_session(BOARD, "pytest-missing"), ErrorCode.NOT_FOUND)
    rejected("drf_missing", BOARD, ErrorCode.FORBIDDEN_ROLE)


@pytest.mark.parametrize("draft_id", ["drf_missing", "drf\x00x", ""])
def test_an_unknown_draft_is_not_found(draft_id):
    rejected(draft_id, APPROVER, ErrorCode.NOT_FOUND)


def test_another_sessions_draft_is_unknown():
    session_id = new_session()
    theirs = create_incident({"title": "Theirs", "discovery_date": "2026-10-01"},
                             **in_session(RESPONDER, session_id))
    draft_id = add_draft(theirs["incident_id"])
    rejected(draft_id, APPROVER, ErrorCode.NOT_FOUND)
    # In its own session it can be accepted.
    assert accept_draft(draft_id, **in_session(APPROVER, session_id))["status"] == "Accepted"


def test_competing_acceptances_save_once(draft):
    """The row lock makes the second request wait, then find the draft already accepted."""
    incident_id, draft_id = draft
    ready = Barrier(2)

    def submit(_):
        ready.wait(timeout=10)
        try:
            accept_draft(draft_id, **APPROVER)
            return "saved"
        except TracerError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sorted(results, key=str) == sorted(["saved", ErrorCode.CONFLICT], key=str)
    actions = [entry.action for entry in audit_entries(incident_id)]
    assert actions.count(AuditAction.ACCEPT_DRAFT) == 1
