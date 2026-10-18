"""The tester's cross-team cases, run against the in-memory system in fake_system.py."""

import time
from datetime import UTC, datetime, timedelta
from email.utils import formatdate

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tester.cases import STATES, clock_problems, name, planned
from tester.client import Reply, Target
from tester.spec import response_errors, schema_errors
from tester.suite import Fail, run_cases
from tests.fake_system import FakeSystem

# Target sets the 10 s call timeout on every request; TestClient warns that it ignores it.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

CRITERIA = ["AC-02", "AC-03", "AC-07", "AC-08", "AC-20", "AC-06", "AC-09", "AC-23", "AC-01"]
FILING_PAIRS = {
    *(f"AC-06-{source}-to-{name(to)}" for source in ("DisclosureRecorded", "AmendmentPending")
      for to in STATES),
    "AC-06-DM-to-DisclosureRecorded",
    "AC-06-DisclosureDelayed-to-DisclosureRecorded",
}


class StaleDate:
    """An HTTP client whose responses carry a Date header an hour old, like a host that cached it."""

    def __init__(self, http: TestClient):
        self.http = http

    def request(self, method: str, url: str, **kwargs):
        response = self.http.request(method, url, **kwargs)
        response.headers["date"] = formatdate(time.time() - 3600, usegmt=True)
        return response


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
def conforming() -> dict[str, dict]:
    return run_against(FakeSystem())


def test_a_conforming_system_passes_every_case_that_runs(conforming):
    skipped = {test_id for test_id, r in conforming.items() if r["outcome"] == "skip"}
    assert skipped == FILING_PAIRS
    others = {t: r["message"] for t, r in conforming.items()
              if r["outcome"] not in ("pass", "skip")}
    assert others == {}


def test_every_result_is_a_valid_test_result(conforming):
    for result in conforming.values():
        assert schema_errors("TestResult", result) == []
        assert result["message"].strip()


def test_test_ids_are_unique_and_in_criterion_order():
    ids = [test_id for test_id, _ in planned()]
    assert len(ids) == len(set(ids))
    assert list(dict.fromkeys(test_id[:5] for test_id in ids)) == CRITERIA


def test_ac06_plans_all_49_pairs_and_runs_33(conforming):
    pairs = {f"AC-06-{name(source)}-to-{name(to)}" for source in STATES for to in STATES}
    assert len(pairs) == 49
    assert pairs <= set(conforming)
    assert sum(conforming[p]["outcome"] == "pass" for p in pairs) == 33


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
    ],
)
def test_cases_fail_a_faulty_system(bug, test_id):
    results = run_against(FakeSystem({bug}), test_id[:5])
    assert results[test_id]["outcome"] == "fail", results[test_id]


def test_a_wrongly_allowed_move_doesnt_spoil_later_cases_from_that_state():
    results = run_against(FakeSystem({"dnm_to_dm"}), "AC-06")
    assert results["AC-06-DNM-to-DM"]["outcome"] == "fail"
    later = ["AC-06-DNM-to-DNM", "AC-06-DNM-to-DM-via-determination", "AC-06-DNM-to-Closed"]
    assert [results[t]["outcome"] for t in later] == ["pass"] * 3


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
