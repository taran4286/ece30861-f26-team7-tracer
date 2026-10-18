"""app/services/control.py: session setup, the coverage report and the snapshot."""

import uuid
from datetime import UTC, datetime

import pytest

from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import RegistrantType, TestSession
from app.services import control
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from app.services.workflow import list_audit_entries
from tests.helpers import BOARD, RESPONDER, alert, in_session, new_session, table_counts

pytestmark = pytest.mark.usefixtures("default_session")

# Valid as an ID, and never stored: every body that uses it is rejected.
UNUSED_ID = "pytest-never-stored"


def new_id() -> str:
    return f"pytest-{uuid.uuid4().hex}"


def stored(session_id: str) -> TestSession | None:
    with transaction() as tx:
        return tx.get(TestSession, session_id)


def test_empty_setup_stores_the_pod_defaults():
    session_id = new_id()
    assert control.setup_session({"session_id": session_id, "setup": {}}) == {
        "session_id": session_id
    }
    session = stored(session_id)
    assert session.test_now == datetime(2026, 10, 5, 15, tzinfo=UTC)
    assert session.registrant_type == RegistrantType.DOMESTIC
    assert session.at_risk_threshold_days == 7
    assert session.llm_base_url == "https://llm.invalid/v1"


def test_setup_stores_the_settings_sent():
    session_id = new_id()
    control.setup_session({"session_id": session_id, "setup": {
        "test_now": "2026-10-08T09:30:00-04:00", "registrant_type": "FPI",
        "at_risk_threshold_days": 3.0, "llm_base_url": "http://llm.example/v1",
    }})
    session = stored(session_id)
    assert session.test_now == datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
    assert session.registrant_type == RegistrantType.FPI
    assert session.at_risk_threshold_days == 3
    assert session.llm_base_url == "http://llm.example/v1"


def test_a_session_id_already_in_use_is_a_conflict():
    session_id = new_session()
    before = table_counts()
    with pytest.raises(TracerError) as error:
        control.setup_session({"session_id": session_id, "setup": {"registrant_type": "FPI"}})
    assert error.value.code == ErrorCode.CONFLICT
    assert table_counts() == before
    assert stored(session_id).registrant_type == RegistrantType.DOMESTIC


@pytest.mark.parametrize("body,field", [
    (None, None),
    ([], None),
    ({"setup": {}}, "session_id"),
    ({"session_id": "", "setup": {}}, "session_id"),
    ({"session_id": "   ", "setup": {}}, "session_id"),
    ({"session_id": 7, "setup": {}}, "session_id"),
    ({"session_id": "nul\x00id", "setup": {}}, "session_id"),
    ({"session_id": UNUSED_ID}, "setup"),
    ({"session_id": UNUSED_ID, "setup": None}, "setup"),
    ({"session_id": UNUSED_ID, "setup": []}, "setup"),
    ({"session_id": UNUSED_ID, "setup": {"clock": "x"}}, "setup.clock"),
    ({"session_id": UNUSED_ID, "setup": {"test_now": "2026-10-05"}}, "setup.test_now"),
    ({"session_id": UNUSED_ID, "setup": {"test_now": "2026-10-05T15:00:00"}}, "setup.test_now"),
    ({"session_id": UNUSED_ID, "setup": {"test_now": None}}, "setup.test_now"),
    ({"session_id": UNUSED_ID, "setup": {"registrant_type": "domestic"}}, "setup.registrant_type"),
    ({"session_id": UNUSED_ID, "setup": {"registrant_type": None}}, "setup.registrant_type"),
    *(({"session_id": UNUSED_ID, "setup": {"at_risk_threshold_days": value}},
       "setup.at_risk_threshold_days") for value in (0, -1, 1.5, "7", True, None)),
    *(({"session_id": UNUSED_ID, "setup": {"llm_base_url": value}}, "setup.llm_base_url")
      for value in ("llm.example", "http://llm example", "", 5, None)),
])
def test_invalid_setup_stores_nothing(body, field):
    before = table_counts()
    with pytest.raises(TracerError) as error:
        control.setup_session(body)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, field)
    assert table_counts() == before


def test_coverage_report_has_null_counts_and_a_reason():
    session_id = new_session()
    unmeasured = {"covered": None, "total": None, "basis": control.CODE_COVERAGE_BASIS}
    assert control.coverage_report(session_id) == {
        "session_id": session_id, "coverage": {"code": unmeasured}
    }


@pytest.mark.parametrize("session_id,code", [
    (None, ErrorCode.VALIDATION_ERROR),
    ("", ErrorCode.VALIDATION_ERROR),
    ("  ", ErrorCode.VALIDATION_ERROR),
    ("pytest-missing", ErrorCode.NOT_FOUND),
])
def test_coverage_report_needs_a_known_session(session_id, code):
    with pytest.raises(TracerError) as error:
        control.coverage_report(session_id)
    assert error.value.code == code


def test_snapshot_holds_only_its_sessions_records():
    mine, other = new_session(registrant_type=RegistrantType.FPI), new_session()
    me, reader = in_session(RESPONDER, mine), in_session(BOARD, mine)
    first = create_incident({"title": "First", "discovery_date": "2026-10-01"}, **me)
    second = create_incident({"title": "Second", "discovery_date": "2026-10-02"}, **me)
    import_evidence(first["incident_id"], {"source": "siem", "items": [alert(1), alert(2)]}, **me)
    create_incident({"title": "Elsewhere", "discovery_date": "2026-10-01"},
                    **in_session(RESPONDER, other))
    before = table_counts()

    snapshot = control.snapshot(mine)

    assert table_counts() == before
    assert snapshot["run"] == {
        "run_id": mine, "test_now": "2026-10-05T15:00:00+00:00", "registrant_type": "FPI",
        "at_risk_threshold_days": 7, "llm_base_url": "https://llm.invalid/v1",
        "implemented_optional": [],
    }
    ids = sorted([first["incident_id"], second["incident_id"]])
    assert snapshot["incidents"] == [get_incident(i, **reader) for i in ids]
    assert snapshot["groups"] == []
    # Oldest first across the session: first's create, second's create, then the import.
    trails = {i: list_audit_entries(i, **reader)["entries"] for i in ids}
    entries = [*trails[first["incident_id"]], *trails[second["incident_id"]]]
    assert sorted(snapshot["audit"], key=lambda e: e["entry_id"]) == sorted(
        entries, key=lambda e: e["entry_id"]
    )
    assert [e["action"] for e in snapshot["audit"]] == [
        "create_incident", "create_incident", "import_evidence"
    ]


def test_snapshot_of_a_new_session_is_empty():
    session_id = new_session()
    snapshot = control.snapshot(session_id)
    assert (snapshot["incidents"], snapshot["groups"], snapshot["audit"]) == ([], [], [])


@pytest.mark.parametrize("run_id", ["pytest-missing", "nul\x00id"])
def test_snapshot_of_an_unknown_session_is_not_found(run_id):
    with pytest.raises(TracerError) as error:
        control.snapshot(run_id)
    assert error.value.code == ErrorCode.NOT_FOUND
