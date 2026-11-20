"""app/services/placeholders.py: a 405 comes only after the session, role and path checks."""

import pytest

from app.errors import ErrorCode, TracerError
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from app.services.placeholders import refuse_change
from tests.helpers import APPROVER, BOARD, RESPONDER, alert, in_session, table_counts

pytestmark = pytest.mark.usefixtures("default_session")


@pytest.fixture
def evidence_id(incident) -> str:
    imported = import_evidence(incident["incident_id"], {"source": "siem", "items": [alert()]},
                               **RESPONDER)
    return imported["evidence"][0]["evidence_id"]


def rejected(call, code, incident_id=None):
    """Run a placeholder call, check its error, and check that nothing was stored or changed."""
    before = table_counts()
    previous = get_incident(incident_id, **BOARD) if incident_id else None
    with pytest.raises(TracerError) as error:
        call()
    assert error.value.code == code
    assert table_counts() == before
    if incident_id:
        assert get_incident(incident_id, **BOARD) == previous


@pytest.mark.parametrize("who,code", [
    (APPROVER, ErrorCode.METHOD_NOT_ALLOWED),
    (RESPONDER, ErrorCode.FORBIDDEN_ROLE),
    (BOARD, ErrorCode.FORBIDDEN_ROLE),
])
def test_only_a_disclosure_approver_reaches_the_405(incident, evidence_id, who, code):
    incident_id = incident["incident_id"]
    rejected(lambda: refuse_change(incident_id, **who), code, incident_id)
    rejected(lambda: refuse_change(incident_id, evidence_id, **who), code, incident_id)


def test_refusal_checks_the_session_first():
    rejected(lambda: refuse_change("inc_missing", **in_session(BOARD, "pytest-missing")),
             ErrorCode.NOT_FOUND)


def test_refusal_looks_up_the_incident_and_evidence(incident, evidence_id):
    incident_id = incident["incident_id"]
    rejected(lambda: refuse_change("inc_missing", **APPROVER), ErrorCode.NOT_FOUND)
    rejected(lambda: refuse_change(incident_id, "ev_missing", **APPROVER), ErrorCode.NOT_FOUND,
             incident_id)
    # Evidence of another incident in the same session counts as unknown.
    other = create_incident({"title": "Other", "discovery_date": "2026-10-01"}, **RESPONDER)
    rejected(lambda: refuse_change(other["incident_id"], evidence_id, **APPROVER),
             ErrorCode.NOT_FOUND, other["incident_id"])
