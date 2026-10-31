"""The tester's cross-team cases, run against the in-memory system in fake_system.py."""

import time
from datetime import UTC, date, datetime, timedelta
from email.utils import formatdate

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tester import business_days
from tester.cases import SETUP_TEST_NOW, STATES, clock_problems, name, planned
from tester.client import Reply, Target
from tester.spec import response_errors, schema_errors
from tester.suite import Fail, run_cases
from tests.fake_system import FakeSystem

# Target sets the 10 s call timeout on every request; TestClient warns that it ignores it.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

CRITERIA = ["AC-02", "AC-03", "AC-07", "AC-08", "AC-20", "AC-14", "AC-12", "AC-13", "AC-15",
            "AC-06", "AC-09", "AC-23", "AC-01"]
SETTINGS = "/test/sessions/{session_id}/settings"


class StaleDate:
    """An HTTP client whose responses carry a Date header an hour old, like a host that cached it."""

    def __init__(self, http: TestClient):
        self.http = http

    def request(self, method: str, url: str, **kwargs):
        response = self.http.request(method, url, **kwargs)
        response.headers["date"] = formatdate(time.time() - 3600, usegmt=True)
        return response


class Recorded:
    """An HTTP client that records each request's method, URL and JSON body in `sent`."""

    def __init__(self, http: TestClient, sent: list):
        self.http, self.sent = http, sent

    def request(self, method: str, url: str, **kwargs):
        self.sent.append((method, url, kwargs.get("json")))
        return self.http.request(method, url, **kwargs)


class TimesOut:
    """An HTTP client whose calls to paths containing `fragment` never answer."""

    def __init__(self, http: TestClient, fragment: str):
        self.http, self.fragment = http, fragment

    def request(self, method: str, url: str, **kwargs):
        if self.fragment in url:
            raise httpx2.ReadTimeout("timed out")
        return self.http.request(method, url, **kwargs)


def run_against(fake: FakeSystem, criterion: str | None = None, wrap=None) -> dict[str, dict]:
    """Run the planned cases (or one criterion's) against `fake` served under /system."""
    server = FastAPI()
    server.mount("/system", fake.app)
    http = TestClient(server, base_url="http://target/system")
    assert http.post("/test/setup", json={"session_id": "run-1", "setup": {}}).status_code == 201
    cases = [c for c in planned() if criterion is None or c[0].startswith(f"{criterion}-")]
    target = Target(wrap(http) if wrap else http, "run-1")
    return {result["test_id"]: result for result in run_cases(cases, target)}


@pytest.fixture(scope="module")
def conforming_system() -> tuple[FakeSystem, list]:
    """A conforming system, and every request the full run below sends it."""
    return FakeSystem(), []


@pytest.fixture(scope="module")
def conforming(conforming_system) -> dict[str, dict]:
    fake, sent = conforming_system
    return run_against(fake, wrap=lambda http: Recorded(http, sent))


def test_a_conforming_system_passes_every_case(conforming):
    assert {t: r["message"] for t, r in conforming.items() if r["outcome"] != "pass"} == {}


def test_every_result_is_a_valid_test_result(conforming):
    for result in conforming.values():
        assert schema_errors("TestResult", result) == []
        assert result["message"].strip()


def test_test_ids_are_unique_and_in_criterion_order():
    ids = [test_id for test_id, _ in planned()]
    assert len(ids) == len(set(ids))
    assert list(dict.fromkeys(test_id[:5] for test_id in ids)) == CRITERIA


def test_ac06_runs_all_49_pairs(conforming):
    pairs = {f"AC-06-{name(source)}-to-{name(to)}" for source in STATES for to in STATES}
    assert len(pairs) == 49
    assert pairs <= set(conforming)
    assert sum(conforming[p]["outcome"] == "pass" for p in pairs) == 49


def test_every_clock_move_is_set_back_to_the_setup_default(conforming, conforming_system):
    fake, sent = conforming_system
    moves: dict[str, list[str]] = {}
    for method, url, body in sent:
        if method == "PATCH" and url.endswith("/settings"):
            assert set(body) == {"test_now"}
            moves.setdefault(url, []).append(body["test_now"])
    assert set(moves) == {SETTINGS.format(session_id="run-1"),
                          SETTINGS.format(session_id="run-1-fpi")}
    for days in moves.values():
        # Each move to another day is followed by a move back, and by nothing else.
        assert len(days) % 2 == 0
        assert days[1::2] == [SETUP_TEST_NOW] * (len(days) // 2)
        assert SETUP_TEST_NOW not in days[::2]
    assert {s: fake.sessions[s].run["test_now"] for s in ("run-1", "run-1-fpi")} == {
        "run-1": SETUP_TEST_NOW, "run-1-fpi": SETUP_TEST_NOW}


def test_fpi_cases_act_in_a_second_session_prepared_as_fpi(conforming, conforming_system):
    fake, _ = conforming_system
    main, fpi = fake.sessions["run-1"], fake.sessions["run-1-fpi"]
    assert (main.run["registrant_type"], fpi.run["registrant_type"]) == ("Domestic", "FPI")
    assert [i["required_form"] for i in fpi.incidents.values()] == ["6-K"]
    assert "6-K" not in {i["required_form"] for i in main.incidents.values()}


@pytest.mark.parametrize(
    ("bug", "test_id"),
    [
        ("stale_flags", "AC-12-warning-1-business-day-left"),
        ("settings_not_built", "AC-12-warning-1-business-day-left"),
        ("fpi_flags", "AC-13-no-warning-or-overdue"),
        # The transition inside AC-15's clock move fails: no filing lists impact as unavailable.
        ("filing_not_stored", "AC-15-amendment-pending"),
    ],
)
def test_a_failed_clock_case_still_sets_the_clock_back(bug, test_id):
    fake = FakeSystem({bug})
    results = run_against(fake, test_id[:5])
    assert results[test_id]["outcome"] == "fail", results[test_id]
    assert {s.run["test_now"] for s in fake.sessions.values()} == {SETUP_TEST_NOW}


def test_the_fake_systems_settings_route_follows_the_spec():
    http = TestClient(FakeSystem().app)
    assert http.post("/test/setup", json={"session_id": "s", "setup": {}}).status_code == 201
    url = SETTINGS.format(session_id="s")
    before = http.get("/test/report/s").json()["run"]
    for body in ({"registrant_type": "FPI"}, {"theme": "dark"}, {"test_now": "2026-10-08"},
                 {"at_risk_threshold_days": 0}):
        response = http.patch(url, json=body)
        assert response.status_code == 400, body
        assert response_errors("PATCH", SETTINGS, 400, response.json()) == []
    assert http.get("/test/report/s").json()["run"] == before
    response = http.patch(url, json={"test_now": "2026-10-08T15:00:00Z"})
    assert response.status_code == 200
    assert response_errors("PATCH", SETTINGS, 200, response.json()) == []
    assert response.json() == {**before, "test_now": "2026-10-08T15:00:00Z"}
    assert http.patch(SETTINGS.format(session_id="other"), json={}).status_code == 404


@pytest.mark.parametrize(
    ("start", "due"),
    [
        ("2026-10-05", "2026-10-09"),  # AC-12
        ("2026-11-09", "2026-11-16"),  # AC-15: Veterans Day, Wednesday 2026-11-11
        ("2026-10-08", "2026-10-15"),  # Columbus Day, Monday 2026-10-12
        ("2026-10-03", "2026-10-08"),  # a Saturday start
        ("2027-06-17", "2027-06-24"),  # Juneteenth on a Saturday, observed Friday 2027-06-18
        ("2021-12-29", "2022-01-05"),  # New Year's Day 2022 observed Friday 2021-12-31
    ],
)
def test_the_testers_calendar_counts_4_business_days(start, due):
    assert business_days.add_business_days(date.fromisoformat(start)) == date.fromisoformat(due)


@pytest.mark.parametrize(
    ("bug", "test_id"),
    [
        ("imported_at_test_now", "AC-02-ticketing-import-recorded"),
        ("partial_import", "AC-20-import-ticketing-missing-summary"),
        ("future_discovery", "AC-20-create-discovery-tomorrow"),
        ("responder_determines", "AC-08-Responder-forbidden"),
        ("dnm_to_dm", "AC-06-DNM-to-DM"),
        ("transitions_determine", "AC-06-UA-to-DM-via-transitions"),
        ("transitions_determine", "AC-09-a-incident-01"),
        ("audited_reads", "AC-23-a-reads-not-audited"),
        ("audit_delete", "AC-23-b-DELETE-audit-as-DisclosureApprover"),
        ("accession_optional", "AC-14-missing-accession_number"),
        ("filing_date_bumps_version", "AC-14-missing-filing_date"),
        ("filing_keeps_state", "AC-14-filing-recorded"),
        ("filing_not_stored", "AC-14-filing-retrievable"),
        ("due_date_counts_start", "AC-12-due-date-4-business-days"),
        ("early_warning", "AC-12-no-flag-2-business-days-left"),
        ("stale_flags", "AC-12-warning-1-business-day-left"),
        ("overdue_on_due_date", "AC-12-warning-on-due-date"),
        ("stale_flags", "AC-12-overdue-after-due-date"),
        ("settings_not_built", "AC-12-overdue-after-due-date"),
        ("filing_keeps_flags", "AC-12-filing-clears-flags"),
        ("fpi_gets_8k", "AC-13-material-requires-6-K"),
        ("fpi_flags", "AC-13-no-warning-or-overdue"),
        ("home_date_optional", "AC-13-6-K-missing-home_disclosure_date"),
        ("home_date_dropped", "AC-13-6-K-stores-home_disclosure_date"),
        ("no_amendments", "AC-15-amendment-pending"),
        ("amendment_keeps_form", "AC-15-amendment-requires-8-K-A"),
        ("due_date_ignores_holidays", "AC-15-due-date-skips-Veterans-Day"),
        ("filing_keeps_state", "AC-06-DM-to-DisclosureRecorded"),
        ("recorded_reopens", "AC-06-DisclosureRecorded-to-DM"),
        ("no_amendments", "AC-06-DisclosureRecorded-to-AmendmentPending"),
        ("amendment_closes", "AC-06-AmendmentPending-to-Closed"),
        ("amendment_keeps_form", "AC-06-AmendmentPending-to-DisclosureRecorded"),
    ],
)
def test_cases_fail_a_faulty_system(bug, test_id):
    results = run_against(FakeSystem({bug}), test_id[:5])
    assert results[test_id]["outcome"] == "fail", results[test_id]


@pytest.mark.parametrize(
    ("bug", "failed", "later"),
    [
        ("dnm_to_dm", "AC-06-DNM-to-DM",
         ["AC-06-DNM-to-DNM", "AC-06-DNM-to-DM-via-determination", "AC-06-DNM-to-Closed"]),
        ("recorded_reopens", "AC-06-DisclosureRecorded-to-DM",
         ["AC-06-DisclosureRecorded-to-DNM", "AC-06-DisclosureRecorded-to-AmendmentPending",
          "AC-06-DisclosureRecorded-to-Closed"]),
    ],
)
def test_a_wrongly_allowed_move_doesnt_spoil_later_cases_from_that_state(bug, failed, later):
    results = run_against(FakeSystem({bug}), "AC-06")
    assert results[failed]["outcome"] == "fail"
    assert [results[t]["outcome"] for t in later] == ["pass"] * len(later)


def test_clock_checks_bracket_the_call_with_the_testers_clock():
    sent = datetime(2026, 10, 18, 12, tzinfo=UTC)
    # A Date header years off changes nothing: only the tester's own clock counts.
    headers = httpx2.Headers({"date": "Mon, 01 Jan 2001 00:00:00 GMT"})
    reply = Reply("POST /x", 201, headers, None, sent, sent + timedelta(seconds=2))
    inside = ["2026-10-18T11:59:55Z", "2026-10-18T12:00:01Z", "2026-10-18T12:00:07+00:00",
              "2026-10-18T08:00:03-04:00"]
    assert [clock_problems("t", value, reply) for value in inside] == [[]] * 4
    assert "before the request was sent" in clock_problems("t", "2026-10-18T11:59:54Z", reply)[0]
    assert "after the response arrived" in clock_problems("t", "2026-10-18T12:00:08Z", reply)[0]
    assert clock_problems("t", "2026-10-18T12:00:01", reply) == [
        "t 2026-10-18T12:00:01 has no UTC offset"]


@pytest.mark.parametrize("criterion", ["AC-02", "AC-23"])
def test_a_stale_date_header_fails_no_case(criterion):
    results = run_against(FakeSystem(), criterion, wrap=StaleDate)
    assert {t: r["message"] for t, r in results.items() if r["outcome"] != "pass"} == {}


def test_a_server_error_is_an_error_from_the_system():
    result = run_against(FakeSystem({"board_import_crashes"}), "AC-02")
    assert result["AC-02-import-as-Board-forbidden"]["outcome"] == "error"
    assert result["AC-02-import-as-Board-forbidden"]["source"] == "system"


def test_no_response_is_an_error_from_communication():
    results = run_against(FakeSystem(), "AC-08", wrap=lambda http: TimesOut(http, "/determination"))
    assert results["AC-08-DisclosureApprover-allowed"]["outcome"] == "error"
    assert results["AC-08-DisclosureApprover-allowed"]["source"] == "communication"


def test_a_failed_setup_fails_every_case_that_needs_it():
    def broken(run):
        raise Fail("POST /incidents: expected 201, got 501")

    cases = [(f"case-{n}", lambda run: run.once("shared", lambda: broken(run))) for n in (1, 2)]
    results = run_cases(cases, Target(httpx2.Client(), "run-1"))
    assert [r["outcome"] for r in results] == ["fail", "fail"]
    assert all(r["message"].startswith("Setup failed: POST /incidents") for r in results)


def test_a_tester_fault_is_an_error_from_the_tester():
    results = run_cases([("broken", lambda run: {}["missing"])], Target(httpx2.Client(), "run-1"))
    assert results[0]["outcome"] == "error"
    assert results[0]["source"] == "tester"


def test_response_bodies_are_checked_against_the_spec():
    incident = {"incident_id": "i", "title": "t", "discovery_date": "2026-10-05", "flags": [],
                "filings": [], "evidence": [], "version": 1}
    valid = {**incident, "state": "UnderAssessment"}
    assert response_errors("POST", "/incidents", 201, valid) == []
    assert response_errors("POST", "/incidents", 201, incident) == [
        "$: 'state' is a required property"
    ]
    assert response_errors("POST", "/incidents", 400, {"error": "VALIDATION_ERROR"}) == [
        "$: 'message' is a required property"
    ]
