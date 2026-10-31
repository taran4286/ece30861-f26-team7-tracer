"""app/services/control.py: session setup, the coverage report and the snapshot."""

import uuid
from datetime import UTC, datetime

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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


@pytest.mark.parametrize("uri", [
    "http://[::1]:8080/v1", "https://llm.example/v1?model=a&n=1#top", "https://llm.example/%41pi",
])
def test_setup_stores_any_rfc_3986_absolute_uri(uri):
    session_id = new_id()
    control.setup_session({"session_id": session_id, "setup": {"llm_base_url": uri}})
    assert stored(session_id).llm_base_url == uri


def test_the_largest_storable_threshold_is_accepted():
    session_id = new_id()
    control.setup_session({"session_id": session_id,
                           "setup": {"at_risk_threshold_days": control.INTEGER_MAX}})
    assert stored(session_id).at_risk_threshold_days == control.INTEGER_MAX


def test_a_session_id_already_in_use_is_a_conflict():
    session_id = new_session()
    before = table_counts()
    with pytest.raises(TracerError) as error:
        control.setup_session({"session_id": session_id, "setup": {"registrant_type": "FPI"}})
    assert error.value.code == ErrorCode.CONFLICT
    assert table_counts() == before
    assert stored(session_id).registrant_type == RegistrantType.DOMESTIC


# Setting values that setup and the settings update both reject, as (key, value).
INVALID_SETTINGS = [
    ("test_now", "2026-10-05"),
    ("test_now", "2026-10-05T15:00:00"),
    ("test_now", None),
    ("registrant_type", "domestic"),
    ("registrant_type", None),
    *(("at_risk_threshold_days", value)
      for value in (0, -1, 1.5, "7", True, None, control.INTEGER_MAX + 1, 10**12, 1e30)),
    *(("llm_base_url", value)
      for value in ("llm.example", "http://llm example", "", 5, None, "http://llm.example/\x00",
                    "https:", "https://example.com/%ZZ", "https://exa<mple.com/v1",
                    "https://llm.example/v1#a#b")),
]


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
    *(({"session_id": UNUSED_ID, "setup": {key: value}}, f"setup.{key}")
      for key, value in INVALID_SETTINGS),
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


# --- Settings update ---------------------------------------------------------

# A valid change to every mutable setting, which a rejected update must not apply.
VALID_UPDATE = {
    "test_now": "2026-10-09T00:00:00Z", "at_risk_threshold_days": 2,
    "llm_base_url": "http://llm.example/v2",
}


def run_of(session_id: str) -> dict:
    """The session's settings, as its snapshot reports them."""
    return control.snapshot(session_id)["run"]


@pytest.fixture
def no_database(monkeypatch):
    """Fail the test if the settings update opens a transaction."""
    def forbidden():
        pytest.fail("This update must be rejected before it reaches the database")
    monkeypatch.setattr(control, "transaction", forbidden)


def rejected(session_id, body) -> TracerError:
    with pytest.raises(TracerError) as error:
        control.update_settings(session_id, body)
    return error.value


@pytest.mark.parametrize("key,sent,returned", [
    ("test_now", "2026-10-08T09:30:00-04:00", "2026-10-08T13:30:00+00:00"),
    ("at_risk_threshold_days", 3.0, 3),
    ("llm_base_url", "http://llm.example/v2", "http://llm.example/v2"),
])
def test_a_settings_update_changes_only_the_setting_sent(key, sent, returned):
    session_id = new_session()
    before = run_of(session_id)
    run = control.update_settings(session_id, {key: sent})
    assert run == {**before, key: returned}
    assert run_of(session_id) == run


def test_a_settings_update_changes_every_setting_sent():
    session_id = new_session(registrant_type=RegistrantType.FPI)
    run = control.update_settings(session_id, VALID_UPDATE)
    assert run == {
        "run_id": session_id, "test_now": "2026-10-09T00:00:00+00:00", "registrant_type": "FPI",
        "at_risk_threshold_days": 2, "llm_base_url": "http://llm.example/v2",
        "implemented_optional": [],
    }
    session = stored(session_id)
    assert session.test_now == datetime(2026, 10, 9, tzinfo=UTC)
    assert session.registrant_type == RegistrantType.FPI
    assert (session.at_risk_threshold_days, session.llm_base_url) == (2, "http://llm.example/v2")


def test_an_empty_settings_update_changes_nothing():
    session_id = new_session()
    before, counts = run_of(session_id), table_counts()
    assert control.update_settings(session_id, {}) == before
    assert run_of(session_id) == before
    assert table_counts() == counts


def test_a_settings_update_writes_no_records():
    session_id = new_session()
    me = in_session(RESPONDER, session_id)
    incident = create_incident({"title": "Phishing", "discovery_date": "2026-10-01"}, **me)
    before, counts = control.snapshot(session_id), table_counts()

    control.update_settings(session_id, VALID_UPDATE)

    after = control.snapshot(session_id)
    assert table_counts() == counts
    assert after["audit"] == before["audit"]
    assert [i["incident_id"] for i in after["incidents"]] == [incident["incident_id"]]
    assert after["incidents"][0]["version"] == 1


@pytest.mark.parametrize("body", [
    {"registrant_type": "FPI"},
    {"registrant_type": "Domestic"},
    {"test_now": "2026-10-09T00:00:00Z", "registrant_type": "FPI"},
    {"registrant_type": "FPI", "at_risk_threshold_days": 3},
])
def test_registrant_type_cannot_be_changed(body):
    session_id = new_session()
    before, counts = run_of(session_id), table_counts()
    error = rejected(session_id, body)
    assert (error.code, error.field) == (ErrorCode.VALIDATION_ERROR, "registrant_type")
    assert "can't be changed" in error.message
    assert run_of(session_id) == before
    assert table_counts() == counts


@pytest.mark.parametrize("body,key", [
    ({"clock": "2026-10-09T00:00:00Z"}, "clock"),
    ({"session_id": "pytest-other"}, "session_id"),
    ({**VALID_UPDATE, "setup": {}}, "setup"),
])
def test_an_unknown_setting_changes_nothing(body, key):
    session_id = new_session()
    before = run_of(session_id)
    error = rejected(session_id, body)
    assert (error.code, error.field) == (ErrorCode.VALIDATION_ERROR, key)
    assert repr(key) in error.message
    assert run_of(session_id) == before


@pytest.mark.parametrize("key,value", [(k, v) for k, v in INVALID_SETTINGS
                                       if k != "registrant_type"])
def test_an_invalid_setting_changes_nothing(key, value):
    session_id = new_session()
    before, counts = run_of(session_id), table_counts()
    error = rejected(session_id, {**VALID_UPDATE, key: value})
    assert (error.code, error.field) == (ErrorCode.VALIDATION_ERROR, key)
    assert run_of(session_id) == before
    assert table_counts() == counts


@pytest.mark.parametrize("body", [None, [], [VALID_UPDATE], "test_now", 5])
def test_a_settings_body_that_isnt_an_object_changes_nothing(body):
    session_id = new_session()
    before = run_of(session_id)
    error = rejected(session_id, body)
    assert (error.code, error.field) == (ErrorCode.VALIDATION_ERROR, None)
    assert run_of(session_id) == before


@pytest.mark.parametrize("session_id,body,field", [
    ("", {}, "session_id"),
    ("   ", VALID_UPDATE, "session_id"),
    (None, {}, "session_id"),
    # The body is checked before the session is looked up.
    ("pytest-missing", {"registrant_type": "FPI"}, "registrant_type"),
    ("pytest-missing", None, None),
])
@pytest.mark.usefixtures("no_database")
def test_a_settings_update_checks_the_request_before_the_database(session_id, body, field):
    error = rejected(session_id, body)
    assert (error.code, error.field) == (ErrorCode.VALIDATION_ERROR, field)


@pytest.mark.parametrize("session_id", ["pytest-missing", "nul\x00id"])
@pytest.mark.parametrize("body", [{}, VALID_UPDATE])
def test_a_settings_update_of_an_unknown_session_is_not_found(session_id, body):
    counts = table_counts()
    assert rejected(session_id, body).code == ErrorCode.NOT_FOUND
    assert table_counts() == counts


def test_date_rules_follow_the_new_clock():
    session_id = new_session()
    me = in_session(RESPONDER, session_id)
    body = {"title": "Found later", "discovery_date": "2026-10-08"}
    with pytest.raises(TracerError) as error:
        create_incident(body, **me)
    assert error.value.field == "discovery_date"

    control.update_settings(session_id, {"test_now": "2026-10-08T15:00:00Z"})

    assert create_incident(body, **me)["discovery_date"] == "2026-10-08"
    with pytest.raises(TracerError) as error:
        create_incident({"title": "Too late", "discovery_date": "2026-10-09"}, **me)
    assert error.value.field == "discovery_date"


# --- Database roles ----------------------------------------------------------


@pytest.mark.parametrize("column,value", [
    ("test_now", "2026-10-08T15:00:00Z"), ("at_risk_threshold_days", "3"),
    ("llm_base_url", "https://llm.invalid/v2"),
])
def test_the_app_role_can_update_the_mutable_settings(column, value):
    session_id = new_session()
    with transaction() as tx:
        tx.execute(
            text(f"UPDATE test_sessions SET {column} = :v WHERE session_id = :s"),
            {"v": value if column != "at_risk_threshold_days" else int(value), "s": session_id},
        )


@pytest.mark.parametrize("column", ["registrant_type", "session_id", "created_at"])
def test_the_app_role_cannot_update_other_session_columns(column):
    # Matches no row, so a wrongly granted UPDATE still changes nothing.
    with pytest.raises(DBAPIError) as error, transaction() as tx:
        tx.execute(text(f"UPDATE test_sessions SET {column} = {column} WHERE session_id = 'none'"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


@pytest.mark.parametrize("statement", [
    "UPDATE filings SET accession_number = accession_number WHERE incident_id = 'none'",
    "DELETE FROM filings WHERE incident_id = 'none'",
])
def test_the_app_role_cannot_change_or_delete_filings(statement):
    with pytest.raises(DBAPIError) as error, transaction() as tx:
        tx.execute(text(statement))
    assert isinstance(error.value.orig, InsufficientPrivilege)
