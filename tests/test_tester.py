"""The tester's photo page, 501 stub, session storage, setup, run and report routes, and the
rule that tester/ never imports app."""

import ast
import base64
import html
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError

from app.db import transaction as app_transaction
from server import app as server_app
from tester import db as tester_db
from tester import pages as tester_pages
from tester.cases import planned
from tester.models import RunStatus, StoredSession
from tester.runner import report_problems
from tester.sessions import ControlError, create_session, finish_run, get_report, start_run
from tester.spec import report_errors
from tests.fake_system import FakeSystem

# The runner sets a timeout on every request; TestClient warns that it ignores it.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

TESTER_DIR = Path(__file__).resolve().parents[1] / "tester"
TARGET = "https://example.test/system"
# Where the runner's patched client_for serves a fake target system.
TARGET_URL = "http://target/system"

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


def stored_session_count() -> int:
    """Rows in tester_sessions, to check a rejected setup stored nothing."""
    with tester_db.transaction() as tx:
        return tx.scalar(select(func.count()).select_from(StoredSession))


@pytest.mark.parametrize("path", ["/tester", "/tester/"])
def test_base_url_shows_how_to_run_the_tester(path, monkeypatch):
    def no_storage(session_id):
        pytest.fail("The page reads storage only for a session ID")

    monkeypatch.setattr("tester.get_report", no_storage)
    # /tester redirects to /tester/; TestClient follows it.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Team 7 — Tracer tester</h1>" in response.text
    assert "Team 7 tester" not in response.text
    assert base64.b64encode((TESTER_DIR / "tester.png").read_bytes()).decode() in response.text
    assert "POST /tester/test/setup" in response.text
    assert "Enter a session ID to see its stored report." in response.text


def test_page_shows_a_finished_runs_report(finished):
    session_id, response = finished
    results = response.json()["results"]
    page = client.get("/tester/", params={"session_id": session_id})
    assert page.status_code == 200
    passed = sum(result["outcome"] == "pass" for result in results)
    assert f'{len(results)} tests, <span class="ok">{passed} pass</span>' in page.text
    for result in results:
        assert f"<code>{html.escape(result['test_id'])}</code>" in page.text
        assert html.escape(result["message"]) in page.text


@pytest.mark.parametrize("session_id,shown", [
    ("t7-missing", "Unknown session &#x27;t7-missing&#x27;"),
    ("t7\x00nul", "Unknown session"),
])
def test_page_says_why_a_report_is_missing(session_id, shown):
    page = client.get("/tester/", params={"session_id": session_id})
    assert page.status_code == 200
    assert shown in page.text


def test_page_says_a_running_session_has_no_report_yet():
    session_id = f"t7-{uuid.uuid4().hex}"
    create_session(session_id, TARGET_URL)
    page = client.get("/tester/", params={"session_id": session_id})
    assert "hasn&#x27;t finished its run" in page.text


def test_page_says_when_storage_is_unavailable(monkeypatch):
    def unreachable(session_id):
        raise OperationalError("SELECT", {}, Exception("connection refused"))

    monkeypatch.setattr("tester.get_report", unreachable)
    page = client.get("/tester/", params={"session_id": "t7-any"})
    assert page.status_code == 200
    assert "Tester storage is unavailable" in page.text


def test_page_escapes_everything_from_a_report():
    unmeasured = {"covered": None, "total": None, "basis": "<b>basis</b>"}
    report = {
        "session_id": "<s>id</s>", "target_url": "https://x/<t>",
        "results": [{"test_id": "AC-02-<x>", "outcome": "fail",
                     "message": "<script>alert(1)</script>"}],
        "coverage": {"code": unmeasured, "component": unmeasured, "system": unmeasured},
    }
    page = tester_pages.render("<i>typed</i>", report, None)
    for raw in ("<script>", "<b>basis", "<s>id", "<t>", "<x>", "<i>typed"):
        assert raw not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_other_tester_paths_return_501(method):
    response = client.request(method, "/tester/test/unknown")
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
    assert response.json() == {"message": "Tester storage is unavailable", "source": "tester"}


# --- POST /tester/test/setup and /tester/test/run ------------------------------


def serve(target: FastAPI) -> Callable[[str], TestClient]:
    """A client_for that reaches `target` mounted at /system, like a system base URL."""
    server = FastAPI()
    server.mount("/system", target)
    return lambda target_url: TestClient(server, base_url=target_url)


class Unanswered:
    """Wraps a client so that requests whose URL contains `fragment` time out."""

    def __init__(self, http: TestClient, fragment: str):
        self.http, self.fragment = http, fragment

    def __enter__(self) -> "Unanswered":
        self.http.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.http.__exit__(*exc_info)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        if self.fragment in url:
            raise httpx2.ReadTimeout("timed out")
        return self.http.request(method, url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx2.Response:
        return self.request("POST", url, **kwargs)


@pytest.fixture
def fake(monkeypatch) -> FakeSystem:
    system = FakeSystem()
    monkeypatch.setattr("tester.runner.client_for", serve(system.app))
    return system


@pytest.fixture
def ac08_only(monkeypatch) -> list:
    """Plans only AC-08's cases, so a run takes a moment."""
    cases = [case for case in planned() if case[0].startswith("AC-08-")]
    monkeypatch.setattr("tester.runner.planned", lambda: cases)
    return cases


def setup_session() -> str:
    response = client.post("/tester/test/setup", json={"target_url": TARGET_URL})
    assert response.status_code == 201, response.json()
    return response.json()["session_id"]


def run(session_id: Any) -> httpx2.Response:
    return client.post("/tester/test/run", json={"session_id": session_id})


def report_of(session_id: str) -> dict[str, Any]:
    response = client.get("/tester/test/report", params={"session_id": session_id})
    assert response.status_code == 200, response.json()
    return response.json()


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"target_url": 5},
        {"target_url": " "},
        {"target_url": "target/system"},
        {"target_url": "ftp://target/system"},
        {"target_url": "http://target/system/test"},
        {"target_url": "http://target/system?run=1"},
        {"target_url": "http://target/system#top"},
        {"target_url": "http://user:secret@target/system"},
    ],
)
def test_setup_rejects_a_bad_target_url(fake, body):
    before = stored_session_count()
    response = client.post("/tester/test/setup", json=body)
    assert response.status_code == 400
    assert response.json()["source"] == "tester"
    assert fake.sessions == {}
    assert stored_session_count() == before


def test_setup_prepares_the_target_with_default_settings_and_stores_a_ready_session(fake):
    session_id = setup_session()
    # setup: {} gives the spec's default test_now, which the cases' dates assume.
    assert fake.sessions[session_id].run["test_now"] == "2026-10-05T15:00:00Z"
    row = stored(session_id)
    assert row.status == RunStatus.READY
    assert row.target_url == TARGET_URL


def test_setup_passes_on_the_targets_error(monkeypatch):
    target = FastAPI()

    @target.post("/test/setup")
    def waking_up() -> JSONResponse:
        return JSONResponse({"message": "Database is waking up", "source": "system"}, 503)

    monkeypatch.setattr("tester.runner.client_for", serve(target))
    before = stored_session_count()
    response = client.post("/tester/test/setup", json={"target_url": TARGET_URL})
    assert response.status_code == 503
    assert response.json() == {"message": "Database is waking up", "source": "system"}
    assert stored_session_count() == before


def test_setup_names_a_target_error_without_a_test_control_body(monkeypatch):
    # An app with no routes answers FastAPI's own 404 body.
    monkeypatch.setattr("tester.runner.client_for", serve(FastAPI()))
    before = stored_session_count()
    response = client.post("/tester/test/setup", json={"target_url": TARGET_URL})
    assert response.status_code == 404
    assert response.json() == {"message": "Target setup returned 404", "source": "system"}
    assert stored_session_count() == before


def test_setup_without_an_answer_is_a_communication_error(monkeypatch):
    target = serve(FakeSystem().app)
    monkeypatch.setattr(
        "tester.runner.client_for", lambda url: Unanswered(target(url), "/test/setup")
    )
    before = stored_session_count()
    response = client.post("/tester/test/setup", json={"target_url": TARGET_URL})
    assert response.status_code == 500
    assert response.json()["source"] == "communication"
    assert stored_session_count() == before


@pytest.fixture(scope="module")
def finished() -> tuple[str, httpx2.Response]:
    """One full run against a conforming fake system: its session ID and the run's response."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("tester.runner.client_for", serve(FakeSystem().app))
        session_id = setup_session()
        return session_id, run(session_id)


def test_a_run_returns_and_stores_a_complete_report(finished):
    session_id, response = finished
    assert response.status_code == 200, response.json()
    report = response.json()
    assert report_errors(report) == []
    assert report["session_id"] == session_id
    assert report["target_url"] == TARGET_URL
    assert [r["test_id"] for r in report["results"]] == [test_id for test_id, _ in planned()]
    assert stored(session_id).status == RunStatus.FINISHED
    assert report_of(session_id) == report


def test_a_run_reports_code_component_and_system_coverage(finished):
    coverage = finished[1].json()["coverage"]
    assert coverage["code"] == {
        "covered": None, "total": None, "basis": "The fake system doesn't measure code coverage"
    }
    assert (coverage["component"]["covered"], coverage["component"]["total"]) == (7, 7)
    assert (coverage["system"]["covered"], coverage["system"]["total"]) == (9, 9)


def test_a_session_runs_only_once(finished):
    response = run(finished[0])
    assert response.status_code == 409
    assert response.json()["source"] == "tester"


@pytest.mark.parametrize("session_id", [None, "", "  ", 5])
def test_run_needs_a_session_id(session_id):
    response = run(session_id)
    assert response.status_code == 400
    assert response.json() == {"message": "session_id is required", "source": "tester"}


def test_run_needs_a_json_object_body():
    response = client.post("/tester/test/run", content=b"not json")
    assert response.status_code == 400
    assert response.json()["source"] == "tester"


def test_run_rejects_an_unknown_session():
    response = run(f"run-{uuid.uuid4().hex}")
    assert response.status_code == 404
    assert response.json()["source"] == "tester"


def test_a_run_past_its_deadline_is_aborted_with_its_partial_report(fake, ac08_only, monkeypatch):
    monkeypatch.setattr("tester.runner.CALL_CUTOFF_SECONDS", 0)
    session_id = setup_session()
    response = run(session_id)
    assert response.status_code == 500
    assert response.json()["source"] == "tester"
    assert stored(session_id).status == RunStatus.ABORTED
    first, *rest = report_of(session_id)["results"]
    assert (first["outcome"], first["source"]) == ("error", "tester")
    assert [r["outcome"] for r in rest] == ["skip"] * (len(ac08_only) - 1)


def test_a_run_whose_coverage_report_gets_no_answer_still_finishes(ac08_only, monkeypatch):
    target = serve(FakeSystem().app)
    monkeypatch.setattr(
        "tester.runner.client_for", lambda url: Unanswered(target(url), "?session_id=")
    )
    session_id = setup_session()
    response = run(session_id)
    assert response.status_code == 200, response.json()
    code = response.json()["coverage"]["code"]
    assert (code["covered"], code["total"]) == (None, None)
    assert code["basis"].startswith("The target's coverage report gave no answer")


def test_a_run_that_cant_plan_its_cases_leaves_the_session_ready(fake, monkeypatch):
    session_id = setup_session()

    def unreadable() -> list:
        raise OSError("fixtures/incidents.json is missing")

    monkeypatch.setattr("tester.runner.planned", unreadable)
    response = run(session_id)
    assert response.status_code == 500
    assert response.json() == {"message": "Tester fault: OSError", "source": "tester"}
    assert stored(session_id).status == RunStatus.READY


def test_a_run_whose_report_check_fails_is_stored_as_aborted(fake, ac08_only, monkeypatch):
    def unreadable(report: dict[str, Any], cases: list) -> list[str]:
        raise OSError("tester-openapi.yaml is missing")

    monkeypatch.setattr("tester.runner.report_problems", unreadable)
    session_id = setup_session()
    response = run(session_id)
    assert response.status_code == 500
    assert response.json()["source"] == "tester"
    assert stored(session_id).status == RunStatus.ABORTED
    results = report_of(session_id)["results"]
    assert [r["test_id"] for r in results] == [test_id for test_id, _ in ac08_only]


def test_report_problems_catch_what_the_schema_cant():
    over = {"covered": 2, "total": 1, "basis": "Counted twice"}
    twice = {"test_id": "AC-01-a", "outcome": "pass", "message": "ok"}
    report = {"session_id": "s", "target_url": TARGET_URL, "results": [twice, twice],
              "coverage": {"code": over, "component": over, "system": over}}
    problems = report_problems(report, [("AC-01-a", None), ("AC-01-b", None)])
    assert "a test ID appears more than once" in problems
    assert "the results don't list every planned test" in problems
    assert "code coverage counts more covered than total" in problems


# --- Database roles ----------------------------------------------------------


def test_the_tester_role_cannot_delete_sessions():
    # Matches no row, so a wrongly granted DELETE still removes nothing.
    with pytest.raises(DBAPIError) as error, tester_db.transaction() as tx:
        tx.execute(text("DELETE FROM tester_sessions WHERE session_id = 'none'"))
    assert isinstance(error.value.orig, InsufficientPrivilege)


@pytest.mark.parametrize(
    "table", ["test_sessions", "incidents", "evidence", "determinations", "audit_entries"]
)
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
