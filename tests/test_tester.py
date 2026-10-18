"""The tester's photo page, 501 stub, session storage and report route, and the rule that
tester/ never imports app."""

import ast
import base64
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.db import transaction as app_transaction
from server import app as server_app
from tester import db as tester_db
from tester.models import RunStatus, StoredSession
from tester.sessions import ControlError, create_session, finish_run, get_report, start_run

TESTER_DIR = Path(__file__).resolve().parents[1] / "tester"
TARGET = "https://example.test/system"

client = TestClient(server_app)


def new_session() -> str:
    """Store a fresh ready session. The tester role can't delete, so each test makes its own."""
    session_id = f"run-{uuid.uuid4().hex}"
    create_session(session_id, TARGET)
    return session_id


def suite_report(session_id: str, outcome: str = "pass") -> dict[str, Any]:
    # Storage doesn't check a report's shape; the run checks it before storing it.
    return {
        "session_id": session_id,
        "target_url": TARGET,
        "results": [{"test_id": "AC-01", "outcome": outcome, "message": "100 distinct IDs"}],
        "coverage": {},
    }


def stored(session_id: str) -> StoredSession:
    with tester_db.transaction() as tx:
        return tx.get(StoredSession, session_id)


@pytest.mark.parametrize("path", ["/tester", "/tester/"])
def test_base_url_shows_the_captioned_tester_photo(path):
    # /tester redirects to /tester/; TestClient follows it.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<p style=\"margin:0;font-size:1.25rem\">Team 7 tester</p>" in response.text
    assert base64.b64encode((TESTER_DIR / "tester.png").read_bytes()).decode() in response.text


@pytest.mark.parametrize(("method", "path"), [("POST", "/tester/test/setup"), ("POST", "/tester/test/run")])
def test_tester_paths_return_501(method, path):
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["source"] == "tester"
    assert body["message"]


# --- Session storage ---------------------------------------------------------


def test_a_new_session_is_ready_without_a_report():
    row = stored(new_session())
    assert row.status == RunStatus.READY
    assert row.target_url == TARGET
    assert row.report is None


def test_start_run_returns_the_target_only_once():
    session_id = new_session()
    assert start_run(session_id) == TARGET
    with pytest.raises(ControlError) as error:
        start_run(session_id)
    assert error.value.status == 409
    assert stored(session_id).status == RunStatus.RUNNING


def test_start_run_rejects_an_unknown_session():
    with pytest.raises(ControlError) as error:
        start_run(f"run-{uuid.uuid4().hex}")
    assert error.value.status == 404


def test_only_one_of_two_simultaneous_starts_runs():
    session_id = new_session()
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def start() -> None:
        barrier.wait()
        try:
            outcomes.append(start_run(session_id))
        except ControlError as error:
            outcomes.append(error.status)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count(TARGET) == 1
    assert outcomes.count(409) == 1


def test_finish_run_stores_the_report():
    session_id = new_session()
    start_run(session_id)
    report = suite_report(session_id)
    finish_run(session_id, report)
    row = stored(session_id)
    assert row.status == RunStatus.FINISHED
    assert row.finished_at is not None
    assert get_report(session_id) == report


def test_an_aborted_run_keeps_its_partial_report():
    session_id = new_session()
    start_run(session_id)
    report = suite_report(session_id, outcome="skip")
    finish_run(session_id, report, aborted=True)
    assert stored(session_id).status == RunStatus.ABORTED
    assert get_report(session_id) == report


def test_a_stored_report_is_never_replaced():
    session_id = new_session()
    start_run(session_id)
    report = suite_report(session_id)
    finish_run(session_id, report)
    with pytest.raises(ControlError) as error:
        finish_run(session_id, suite_report(session_id, outcome="fail"))
    assert error.value.status == 500
    assert get_report(session_id) == report


def test_finish_run_needs_a_running_session():
    session_id = new_session()
    with pytest.raises(ControlError) as error:
        finish_run(session_id, suite_report(session_id))
    assert error.value.status == 500
    row = stored(session_id)
    assert row.status == RunStatus.READY
    assert row.report is None


@pytest.mark.parametrize("started", [False, True])
def test_get_report_waits_for_the_run_to_end(started):
    session_id = new_session()
    if started:
        start_run(session_id)
    with pytest.raises(ControlError) as error:
        get_report(session_id)
    assert error.value.status == 409


def test_get_report_rejects_an_unknown_session():
    with pytest.raises(ControlError) as error:
        get_report(f"run-{uuid.uuid4().hex}")
    assert error.value.status == 404


# --- GET /tester/test/report -------------------------------------------------


@pytest.mark.parametrize("params", [{}, {"session_id": ""}, {"session_id": "  "}])
def test_report_route_needs_a_session_id(params):
    response = client.get("/tester/test/report", params=params)
    assert response.status_code == 400
    assert response.json() == {"message": "session_id is required", "source": "tester"}


def test_report_route_rejects_an_unknown_session():
    response = client.get("/tester/test/report", params={"session_id": f"run-{uuid.uuid4().hex}"})
    assert response.status_code == 404
    assert response.json()["source"] == "tester"


def test_report_route_waits_for_the_run_to_end():
    session_id = new_session()
    start_run(session_id)
    response = client.get("/tester/test/report", params={"session_id": session_id})
    assert response.status_code == 409
    assert response.json()["source"] == "tester"


def test_report_route_returns_the_stored_report():
    session_id = new_session()
    start_run(session_id)
    report = suite_report(session_id)
    finish_run(session_id, report)
    response = client.get("/tester/test/report", params={"session_id": session_id})
    assert response.status_code == 200
    assert response.json() == report


def test_report_route_answers_500_when_storage_is_unavailable(monkeypatch):
    def unavailable(session_id: str) -> dict[str, Any]:
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr("tester.get_report", unavailable)
    response = client.get("/tester/test/report", params={"session_id": "run-1"})
    assert response.status_code == 500
    assert response.json() == {"message": "Report storage is unavailable", "source": "tester"}


# --- Database roles ----------------------------------------------------------


def test_the_tester_role_cannot_delete_sessions():
    # Matches no row, so a wrongly granted DELETE still removes nothing.
    with pytest.raises(DBAPIError) as error, tester_db.transaction() as tx:
        tx.execute(text("DELETE FROM tester_sessions WHERE session_id = 'none'"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


@pytest.mark.parametrize("table", ["incidents", "evidence", "determinations", "audit_entries"])
def test_the_tester_role_cannot_read_system_tables(table):
    with pytest.raises(DBAPIError) as error, tester_db.transaction() as tx:
        tx.execute(text(f"SELECT 1 FROM {table}"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


def test_the_app_role_cannot_read_tester_sessions():
    with pytest.raises(DBAPIError) as error, app_transaction() as tx:
        tx.execute(text("SELECT 1 FROM tester_sessions"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


# --- Imports -----------------------------------------------------------------


def _imported_modules(source: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"), filename=str(source))):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return modules


def test_tester_never_imports_app():
    sources = sorted(TESTER_DIR.rglob("*.py"))
    assert sources
    for source in sources:
        for module in _imported_modules(source):
            assert module != "app" and not module.startswith("app."), f"{source} imports {module}"
