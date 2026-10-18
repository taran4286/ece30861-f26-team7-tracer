"""server.py's landing page and mounts, and the system's API over HTTP: health check, test
control, project operations, 405s and 501s, with statuses, error shapes and error order."""

import base64
import re
import socket
import time
import uuid
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api as system_api
import app.db as db
import app.services.control as control_service
from app.config import get_settings
from server import app as server_app
from tester.spec import response_errors

client = TestClient(server_app)


def _clear_cached_settings():
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db._session_factory.cache_clear()


@pytest.fixture
def fresh_settings():
    # Settings, the engine and the session factory are cached (see app/AGENTS.md); clear them
    # around a test that changes the environment, so later tests don't inherit its database.
    _clear_cached_settings()
    yield
    _clear_cached_settings()


def test_health_reaches_the_database():
    response = client.get("/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_a_database_error(monkeypatch):
    def failing_engine():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(system_api, "get_engine", failing_engine)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_health_without_database_url(monkeypatch, tmp_path, fresh_settings):
    # As on a preview deployment: no DATABASE_URL in the environment and no .env file.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_with_a_bare_postgresql_url(monkeypatch, fresh_settings):
    # Neon's connection string starts with postgresql://, which makes SQLAlchemy look for
    # psycopg2 instead of psycopg; the deployment guide says to change the scheme.
    monkeypatch.setenv("DATABASE_URL", "postgresql://tracer_app:x@127.0.0.1:5432/tracer")
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_gives_up_on_a_silent_database(monkeypatch, fresh_settings):
    # A socket that accepts connections but never answers, like a database that stopped responding.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg://tracer_app:x@127.0.0.1:{port}/tracer")
        # psycopg treats anything under 2 seconds as 2.
        monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 2)
        started = time.monotonic()
        response = client.get("/system/health")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert elapsed < 8


@pytest.mark.parametrize("path", ["/system", "/system/"])
def test_base_url_shows_the_captioned_system_photo(path):
    # /system redirects to /system/; TestClient follows it.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<p style=\"margin:0;font-size:1.25rem\">Team 7 service</p>" in response.text
    assert base64.b64encode(system_api.PHOTO.read_bytes()).decode() in response.text


@pytest.mark.parametrize("method,path", [
    *((method, "/system/incidents") for method in ["GET", "PUT", "PATCH", "DELETE"]),
    ("PATCH", "/system/incidents/inc_1"),
    ("POST", "/system/incidents/inc_1/audit"),
    ("GET", "/system/groups"),
    ("POST", "/system/health"),
])
def test_other_system_paths_return_501(method, path):
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NOT_IMPLEMENTED"
    assert body["message"]
    assert "field" not in body


def test_root_links_to_health_system_and_tester():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert re.findall(r'href="([^"]*)"', response.text) == ["/system/health", "/system/", "/tester/"]
    for label in ["Health", "System", "Tester"]:
        assert f">{label}</a>" in response.text


# --- The system's API: helpers -------------------------------------------------------------

SPEC = Path(__file__).resolve().parents[1] / "system-openapi.yaml"
METHODS = {"get", "post", "put", "patch", "delete"}


def new_session(**setup) -> str:
    """Prepare a session through POST /system/test/setup and return its ID."""
    session_id = f"pytest-{uuid.uuid4().hex}"
    response = client.post("/system/test/setup", json={"session_id": session_id, "setup": setup})
    assert response.status_code == 201
    return session_id


def headers(session_id: str, role: str = "Responder") -> dict[str, str]:
    return {"X-User-Id": f"u-{role}", "X-User-Role": role, "X-Test-Run": session_id}


def url(template: str, incident_id: str = "inc_missing", evidence_id: str = "ev_missing") -> str:
    path = template.format(incident_id=incident_id, evidence_id=evidence_id, draft_id="draft_1")
    return f"/system{path}"


def snapshot(session_id: str) -> dict:
    response = client.get(f"/system/test/report/{session_id}")
    assert response.status_code == 200
    return response.json()


def matches_spec(method: str, template: str, response) -> None:
    """The body fits the spec's schema for the operation and status, when the spec gives one."""
    assert response_errors(method, template, response.status_code, response.json()) == []


def control_error(response, status: int) -> None:
    """A TestControlError from the system: exactly message and source."""
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"message", "source"}
    assert body["message"] and body["source"] == "system"


def project_error(response, status: int, error: str, field: str | None = None) -> None:
    """The Error schema, with `field` left out when it has no value."""
    assert response.status_code == status
    body = response.json()
    assert body["error"] == error and body["message"]
    assert body.get("field") == field
    if field is None:
        assert "field" not in body


@pytest.fixture
def no_database(monkeypatch):
    """Fail the test if a request reaches the database."""
    def forbidden():
        pytest.fail("This request must be rejected before it reaches the database")
    monkeypatch.setattr(db, "get_engine", forbidden)
    monkeypatch.setattr(db, "_session_factory", forbidden)


@pytest.fixture
def session_id() -> str:
    return new_session()


@pytest.fixture
def api_incident(session_id) -> dict[str, str]:
    """An UnderAssessment incident with one evidence item, made through the API."""
    created = client.post("/system/incidents", headers=headers(session_id),
                          json={"title": "Ransomware", "discovery_date": "2026-10-01"})
    incident_id = created.json()["incident_id"]
    item = {"ticket_id": "T-1", "summary": "Files locked", "created_at": "2026-10-01T09:00:00Z"}
    imported = client.post(url("/incidents/{incident_id}/evidence/import", incident_id),
                           headers=headers(session_id), json={"source": "ticketing", "items": [item]})
    evidence_id = imported.json()["evidence"][0]["evidence_id"]
    return {"session_id": session_id, "incident_id": incident_id, "evidence_id": evidence_id}


# --- Test control -------------------------------------------------------------------------


def test_setup_coverage_and_snapshot_match_the_spec():
    session_id = f"pytest-{uuid.uuid4().hex}"
    response = client.post("/system/test/setup", json={"session_id": session_id, "setup": {}})
    assert response.status_code == 201
    assert response.json() == {"session_id": session_id}
    matches_spec("POST", "/test/setup", response)

    response = client.get("/system/test/report", params={"session_id": session_id})
    assert response.status_code == 200
    matches_spec("GET", "/test/report", response)
    assert response.json()["coverage"]["code"]["covered"] is None

    response = client.get(f"/system/test/report/{session_id}")
    assert response.status_code == 200
    matches_spec("GET", "/test/report/{run_id}", response)
    assert response.json() == {
        "run": {"run_id": session_id, "test_now": "2026-10-05T15:00:00+00:00",
                "registrant_type": "Domestic", "at_risk_threshold_days": 7,
                "llm_base_url": "https://llm.invalid/v1", "implemented_optional": []},
        "incidents": [], "groups": [], "audit": [],
    }


def test_setup_keeps_the_settings_sent():
    session_id = new_session(test_now="2026-10-09T12:00:00Z", registrant_type="FPI",
                             at_risk_threshold_days=3, llm_base_url="http://llm.example/v1")
    assert snapshot(session_id)["run"] == {
        "run_id": session_id, "test_now": "2026-10-09T12:00:00+00:00", "registrant_type": "FPI",
        "at_risk_threshold_days": 3, "llm_base_url": "http://llm.example/v1",
        "implemented_optional": [],
    }


@pytest.mark.parametrize("method,path,kwargs,status", [
    ("POST", "/system/test/setup",
     {"json": {"session_id": "pytest-never-stored", "setup": {"clock": 1}}}, 400),
    ("POST", "/system/test/setup", {"content": b"{not json"}, 400),
    # Too large for its column: a 400, not a storage error's 500.
    ("POST", "/system/test/setup", {"json": {"session_id": "pytest-never-stored",
                                             "setup": {"at_risk_threshold_days": 10**12}}}, 400),
    ("POST", "/system/test/setup", {"json": {"session_id": " ", "setup": {}}}, 400),
    ("GET", "/system/test/report", {}, 400),
    ("GET", "/system/test/report", {"params": {"session_id": "pytest-missing"}}, 404),
    ("GET", "/system/test/report/pytest-missing", {}, 404),
    ("PATCH", "/system/test/sessions/pytest-missing/settings", {"json": {}}, 501),
])
def test_test_control_errors_use_message_and_source(method, path, kwargs, status):
    control_error(client.request(method, path, **kwargs), status)


def test_setup_with_an_id_in_use_is_a_conflict(session_id):
    response = client.post("/system/test/setup",
                           json={"session_id": session_id, "setup": {"registrant_type": "FPI"}})
    control_error(response, 409)
    assert snapshot(session_id)["run"]["registrant_type"] == "Domestic"


def test_setup_answers_when_storage_is_down(monkeypatch):
    def unreachable():
        raise OperationalError("connect", {}, Exception("connection refused"))

    monkeypatch.setattr(control_service, "transaction", unreachable)
    response = client.post("/system/test/setup",
                           json={"session_id": "pytest-never-stored", "setup": {}})
    control_error(response, 500)
    assert response.json()["message"] == "System storage is unavailable"


# --- Project operations -------------------------------------------------------------------

ROUTES = [
    ("POST", "/incidents"),
    ("GET", "/incidents/{incident_id}"),
    ("PUT", "/incidents/{incident_id}"),
    ("DELETE", "/incidents/{incident_id}"),
    ("POST", "/incidents/{incident_id}/evidence/import"),
    ("PATCH", "/incidents/{incident_id}/evidence/{evidence_id}"),
    ("DELETE", "/incidents/{incident_id}/evidence/{evidence_id}"),
    ("POST", "/incidents/{incident_id}/determination"),
    ("POST", "/incidents/{incident_id}/transitions"),
    ("POST", "/incidents/{incident_id}/filings"),
    ("POST", "/incidents/{incident_id}/drafts"),
    ("POST", "/drafts/{draft_id}/accept"),
    ("GET", "/incidents/{incident_id}/audit"),
    ("PUT", "/incidents/{incident_id}/audit"),
    ("PATCH", "/incidents/{incident_id}/audit"),
    ("DELETE", "/incidents/{incident_id}/audit"),
    ("POST", "/groups"),
]


def test_every_project_operation_in_the_spec_has_a_route():
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    operations = {(method.upper(), path) for path, item in spec["paths"].items()
                  for method in item if method in METHODS and not path.startswith("/test/")}
    assert set(ROUTES) == operations


def test_operations_answer_with_the_spec_shapes(session_id):
    responder, approver = headers(session_id), headers(session_id, "DisclosureApprover")
    board = headers(session_id, "Board")
    created = client.post("/system/incidents", headers=responder,
                          json={"title": "Phishing", "discovery_date": "2026-10-02"})
    assert created.status_code == 201
    matches_spec("POST", "/incidents", created)
    incident_id = created.json()["incident_id"]

    read = client.get(url("/incidents/{incident_id}", incident_id), headers=board)
    assert read.status_code == 200 and read.json() == created.json()
    matches_spec("GET", "/incidents/{incident_id}", read)

    alert = {"alert_id": "A-1", "rule_name": "Beacon", "severity": "high",
             "detected_at": "2026-10-02T08:00:00Z"}
    imported = client.post(url("/incidents/{incident_id}/evidence/import", incident_id),
                           headers=responder, json={"source": "siem", "items": [alert]})
    assert imported.status_code == 201
    matches_spec("POST", "/incidents/{incident_id}/evidence/import", imported)
    evidence_id = imported.json()["evidence"][0]["evidence_id"]

    determined = client.post(url("/incidents/{incident_id}/determination", incident_id),
                             headers=approver, json={
                                 "decision": "NotMaterial", "determination_date": "2026-10-05",
                                 "rationale": "Contained", "evidence_refs": [evidence_id],
                                 "version": 1})
    assert determined.status_code == 200
    assert determined.json()["state"] == "DeterminedNotMaterial"
    matches_spec("POST", "/incidents/{incident_id}/determination", determined)

    closed = client.post(url("/incidents/{incident_id}/transitions", incident_id),
                         headers=approver, json={"to": "Closed", "version": 2})
    assert closed.status_code == 200 and closed.json()["version"] == 3
    matches_spec("POST", "/incidents/{incident_id}/transitions", closed)

    trail = client.get(url("/incidents/{incident_id}/audit", incident_id), headers=board)
    assert trail.status_code == 200
    matches_spec("GET", "/incidents/{incident_id}/audit", trail)
    assert [entry["action"] for entry in trail.json()["entries"]] == [
        "create_incident", "import_evidence", "record_determination", "transition"]
    assert snapshot(session_id)["incidents"] == [closed.json()]


def test_rejections_answer_with_the_error_schema(api_incident):
    session_id, incident_id = api_incident["session_id"], api_incident["incident_id"]
    response = client.post("/system/incidents", headers=headers(session_id),
                           json={"title": "", "discovery_date": "2026-10-01"})
    project_error(response, 400, "VALIDATION_ERROR", "title")
    response = client.post("/system/incidents", headers=headers(session_id), content=b"{oops")
    project_error(response, 400, "VALIDATION_ERROR")
    response = client.post(url("/incidents/{incident_id}/transitions", incident_id),
                           headers=headers(session_id, "DisclosureApprover"),
                           json={"to": "Closed", "version": 1})
    project_error(response, 409, "INVALID_TRANSITION")
    matches_spec("POST", "/incidents/{incident_id}/transitions", response)
    # The incident belongs to its own session; another session can't see it.
    response = client.get(url("/incidents/{incident_id}", incident_id),
                          headers=headers(new_session(), "Board"))
    project_error(response, 404, "NOT_FOUND")


@pytest.mark.parametrize("method,template", ROUTES)
@pytest.mark.parametrize("field,value", [("X-User-Id", None), ("X-User-Id", " "),
    ("X-User-Role", None), ("X-User-Role", "Administrator"), ("X-Test-Run", None),
    ("X-Test-Run", "")])
@pytest.mark.usefixtures("no_database")
def test_invalid_headers_come_first(method, template, field, value):
    sent = headers("pytest-missing", "Board")
    if value is None:
        sent.pop(field)
    else:
        sent[field] = value
    response = client.request(method, url(template), headers=sent)
    project_error(response, 400, "VALIDATION_ERROR", field)
    matches_spec(method, template, response)


@pytest.mark.usefixtures("no_database")
def test_x_user_id_is_reported_first():
    project_error(client.post("/system/groups"), 400, "VALIDATION_ERROR", "X-User-Id")


@pytest.mark.parametrize("method,template", ROUTES)
def test_unknown_session_comes_before_the_role(method, template):
    response = client.request(method, url(template), headers=headers("pytest-missing", "Board"))
    project_error(response, 404, "NOT_FOUND")


@pytest.mark.parametrize("method,template", [r for r in ROUTES if r[0] != "GET"])
def test_role_comes_before_the_incident(session_id, method, template):
    response = client.request(method, url(template), headers=headers(session_id, "Board"))
    project_error(response, 403, "FORBIDDEN_ROLE")


@pytest.mark.parametrize("method,template", [r for r in ROUTES if "{incident_id}" in r[1]])
def test_unknown_incident_comes_before_the_body(session_id, method, template):
    response = client.request(method, url(template),
                              headers=headers(session_id, "DisclosureApprover"))
    project_error(response, 404, "NOT_FOUND")


# --- 405 and 501 ----------------------------------------------------------------------------

REFUSED = [
    ("PUT", "/incidents/{incident_id}", "GET"),
    ("DELETE", "/incidents/{incident_id}", "GET"),
    ("DELETE", "/incidents/{incident_id}/evidence/{evidence_id}", "PATCH"),
    *((method, "/incidents/{incident_id}/audit", "GET") for method in ("PUT", "PATCH", "DELETE")),
]


@pytest.mark.parametrize("method,template,allow", REFUSED)
@pytest.mark.parametrize("role", ["Responder", "DisclosureApprover", "Board"])
def test_only_a_disclosure_approver_reaches_the_405(api_incident, method, template, allow, role):
    session_id = api_incident["session_id"]
    before = snapshot(session_id)
    response = client.request(method, url(template, api_incident["incident_id"],
                                          api_incident["evidence_id"]),
                              headers=headers(session_id, role))
    if role == "DisclosureApprover":
        project_error(response, 405, "METHOD_NOT_ALLOWED")
        assert response.headers["allow"] == allow
    else:
        project_error(response, 403, "FORBIDDEN_ROLE")
        assert "allow" not in response.headers
    matches_spec(method, template, response)
    assert snapshot(session_id) == before


@pytest.mark.parametrize("method", ["DELETE", "PATCH"])
def test_evidence_routes_look_up_the_evidence(api_incident, method):
    response = client.request(method, url("/incidents/{incident_id}/evidence/{evidence_id}",
                                          api_incident["incident_id"], "ev_missing"),
                              headers=headers(api_incident["session_id"], "DisclosureApprover"))
    project_error(response, 404, "NOT_FOUND")


NOT_BUILT = [
    ("PATCH", "/incidents/{incident_id}/evidence/{evidence_id}", {"Responder", "DisclosureApprover"}),
    ("POST", "/groups", {"Responder", "DisclosureApprover"}),
    ("POST", "/incidents/{incident_id}/filings", {"DisclosureApprover"}),
    ("POST", "/incidents/{incident_id}/drafts", {"DisclosureApprover"}),
    ("POST", "/drafts/{draft_id}/accept", {"DisclosureApprover"}),
]


@pytest.mark.parametrize("method,template,allowed", NOT_BUILT)
@pytest.mark.parametrize("role", ["Responder", "DisclosureApprover", "Board"])
def test_unbuilt_operations_answer_501_after_the_role(api_incident, method, template, allowed, role):
    session_id = api_incident["session_id"]
    before = snapshot(session_id)
    response = client.request(method, url(template, api_incident["incident_id"],
                                          api_incident["evidence_id"]),
                              headers=headers(session_id, role))
    if role in allowed:
        project_error(response, 501, "NOT_IMPLEMENTED")
    else:
        project_error(response, 403, "FORBIDDEN_ROLE")
    matches_spec(method, template, response)
    assert snapshot(session_id) == before
