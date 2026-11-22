"""The tester's cross-team cases, run against the in-memory system in fake_system.py."""

import time
from datetime import UTC, date, datetime, timedelta
from email.utils import formatdate

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tester import business_days, stub
from tester.cases import SETUP_TEST_NOW, STATES, clock_problems, name, planned
from tester.client import Reply, Target
from tester.spec import response_errors, schema_errors
from tester.suite import Fail, run_cases
from tests.fake_system import FakeSystem

# Target sets the 10 s call timeout on every request; TestClient warns that it ignores it.
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

CRITERIA = ["AC-21", "AC-02", "AC-03", "AC-07", "AC-08", "AC-20", "AC-14", "AC-12", "AC-13",
            "AC-15", "AC-06", "AC-09", "AC-23", "AC-04", "AC-05", "AC-19", "AC-16", "AC-17",
            "AC-18", "AC-01"]
SETTINGS = "/test/sessions/{session_id}/settings"
# Where the cases tell the fake system to find the tester's LLM stub; its client reaches the
# stub in-process whatever the host.
TESTER_URL = "http://tester/tester"
FAILURE_STUB = stub.fail_url(TESTER_URL)


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


class LlmResetTimesOut:
    """An HTTP client whose first `failures` calls setting the LLM back to the failure stub, after
    AC-21's first case sets it, never answer."""

    def __init__(self, http: TestClient, failures: int):
        self.http, self.failures, self.set_once = http, failures, False

    def request(self, method: str, url: str, **kwargs):
        if method == "PATCH" and (kwargs.get("json") or {}).get("llm_base_url") == FAILURE_STUB:
            if self.set_once and self.failures:
                self.failures -= 1
                raise httpx2.ReadTimeout("timed out")
            self.set_once = True
        return self.http.request(method, url, **kwargs)


class ResetTimesOut:
    """An HTTP client whose calls setting test_now back to the setup default never answer."""

    def __init__(self, http: TestClient):
        self.http = http

    def request(self, method: str, url: str, **kwargs):
        if method == "PATCH" and (kwargs.get("json") or {}).get("test_now") == SETUP_TEST_NOW:
            raise httpx2.ReadTimeout("timed out")
        return self.http.request(method, url, **kwargs)


def run_against(fake: FakeSystem, criterion: str | None = None, wrap=None) -> dict[str, dict]:
    """Run the planned cases (or one criterion's) against `fake` served under /system. A single
    criterion other than AC-21 runs after AC-21's first case, which points the session's LLM at
    the failure stub, as it would in a full run."""
    server = FastAPI()
    server.mount("/system", fake.app)
    http = TestClient(server, base_url="http://target/system")
    assert http.post("/test/setup", json={"session_id": "run-1", "setup": {}}).status_code == 201
    cases = planned()
    if criterion is not None:
        first = [] if criterion == "AC-21" else cases[:1]
        cases = first + [c for c in cases if c[0].startswith(f"{criterion}-")]
    target = Target(wrap(http) if wrap else http, "run-1")
    results = run_cases(cases, target, TESTER_URL)
    return {r["test_id"]: r for r in results if criterion is None or r["test_id"].startswith(
        f"{criterion}-")}


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
        if method == "PATCH" and url.endswith("/settings") and "test_now" in body:
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


def test_every_llm_change_is_set_back_to_the_failure_stub(conforming, conforming_system):
    fake, sent = conforming_system
    urls = [body["llm_base_url"] for method, url, body in sent
            if method == "PATCH" and url == SETTINGS.format(session_id="run-1")
            and "llm_base_url" in body]
    # AC-21's first case sets the failure stub; each draft sets a reply stub, then sets it back.
    assert urls[0] == FAILURE_STUB
    assert len(urls) % 2 == 1
    assert urls[2::2] == [FAILURE_STUB] * (len(urls) // 2)
    assert all(url.startswith(f"{TESTER_URL}/llm/reply/") for url in urls[1::2])
    assert {s: fake.sessions[s].run["llm_base_url"] for s in ("run-1", "run-1-fpi")} == {
        "run-1": FAILURE_STUB, "run-1-fpi": FAILURE_STUB}


def test_drafts_go_through_the_testers_stub(conforming, conforming_system):
    fake, sent = conforming_system
    drafted = [url for method, url, _ in sent if method == "POST" and url.endswith("/drafts")]
    # AC-09 (a)'s ten, AC-21's two, AC-09 (c)'s two, AC-16's fourteen, AC-17's eight, AC-18's five
    assert len(drafted) == 41
    assert conforming["AC-18-detailed-texts-flagged"]["message"].startswith("10/10")
    assert conforming["AC-18-clean-texts-not-flagged"]["message"].startswith("0/10")


def test_a_system_that_repairs_bad_replies_passes_too():
    # The spec allows a 503 or a draft that keeps its rules without what the model got wrong.
    results = run_against(FakeSystem(repairs_drafts=True))
    assert {t: r["message"] for t, r in results.items() if r["outcome"] != "pass"} == {}
    for test_id in ("AC-09-c-model-materiality-not-passed-on",
                    "AC-16-reply-missing-element-not-passed-on",
                    "AC-17-a-other-incident-citation-not-passed-on",
                    "AC-17-a-unknown-citation-not-passed-on",
                    "AC-17-a-uncited-text-not-passed-on"):
        assert results[test_id]["message"].startswith("201 draft without"), results[test_id]


def test_drafting_cases_need_the_testers_url():
    server = FastAPI()
    server.mount("/system", FakeSystem().app)
    http = TestClient(server, base_url="http://target/system")
    http.post("/test/setup", json={"session_id": "run-1", "setup": {}})
    results = run_cases(planned()[:1], Target(http, "run-1"))
    assert (results[0]["outcome"], results[0]["source"]) == ("error", "tester")
    assert "no tester URL" in results[0]["message"]


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


def test_a_failed_llm_reset_is_made_before_the_next_case():
    fake = FakeSystem()
    results = run_against(fake, "AC-16", wrap=lambda http: LlmResetTimesOut(http, 1))
    first, second = results["AC-16-draft-01-has-four-elements"], results[
        "AC-16-draft-02-has-four-elements"]
    assert (first["outcome"], first["source"]) == ("error", "communication"), first
    # The next case sets the failure stub back before it drafts, and passes.
    assert second["outcome"] == "pass", second
    assert fake.sessions["run-1"].run["llm_base_url"] == FAILURE_STUB


def test_a_reset_that_fails_again_fails_the_next_case_and_is_tried_once_more():
    fake = FakeSystem()
    results = run_against(fake, "AC-16", wrap=lambda http: LlmResetTimesOut(http, 2))
    second, third = results["AC-16-draft-02-has-four-elements"], results[
        "AC-16-draft-03-has-four-elements"]
    assert second["outcome"] == "error", second
    assert second["message"].startswith("Before this case, an earlier case's reset failed again")
    assert third["outcome"] == "pass", third
    assert fake.sessions["run-1"].run["llm_base_url"] == FAILURE_STUB


def test_a_failed_reset_is_added_to_a_failed_cases_message():
    # The case fails inside clock_at, and setting the clock back gets no answer either.
    results = run_against(FakeSystem({"flags_not_built"}), "AC-12", wrap=ResetTimesOut)
    result = results["AC-12-filing-clears-flags"]
    assert result["outcome"] == "fail", result
    assert "nothing to clear" in result["message"], result
    assert f"also couldn't set test_now back to {SETUP_TEST_NOW}" in result["message"], result


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
        ("flags_not_built", "AC-12-filing-clears-flags"),
        ("flags_not_built", "AC-12-warning-1-business-day-left"),
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
        ("llm_url_fixed", "AC-21-c-session-uses-failure-stub"),
        ("drafting_message", "AC-21-a-stub-error-drafting-unavailable"),
        ("drafting_message", "AC-21-a-non-JSON-reply-drafting-unavailable"),
        ("draft_moves_incident", "AC-09-c-draft-has-no-materiality-field"),
        ("draft_keeps_materiality", "AC-09-c-model-materiality-not-passed-on"),
        ("group_relinks", "AC-04-second-link-no-duplicate"),
        ("group_self_link", "AC-04-self-link-rejected"),
        ("group_determination_alone", "AC-04-group-determination-on-all"),
        ("misc_tag_accepted", "AC-05-misc-rejected"),
        ("misc_tag_accepted", "AC-05-misc-among-valid-rejected"),
        ("tags_not_stored", "AC-05-five-categories-stored"),
        ("tags_not_stored", "AC-05-tags-replaced-by-approver"),
        ("voluntary_ignored", "AC-19-voluntary-not-material-gives-8-01"),
        ("voluntary_ignored", "AC-19-voluntary-retrievable"),
        ("voluntary_with_material", "AC-19-voluntary-with-Material-rejected"),
        ("draft_drops_impact", "AC-16-draft-01-has-four-elements"),
        ("draft_rewrites_undetermined", "AC-16-draft-03-has-four-elements"),
        ("draft_keeps_missing_element", "AC-16-reply-missing-element-not-passed-on"),
        ("draft_keeps_foreign_citation", "AC-17-a-other-incident-citation-not-passed-on"),
        ("draft_keeps_uncited_text", "AC-17-a-uncited-text-not-passed-on"),
        ("draft_rewrites_undetermined", "AC-17-b-incident-01-impact-not-yet-determined"),
        ("flags_nothing", "AC-18-detailed-texts-flagged"),
        ("draft_adds_citation", "AC-18-detailed-texts-flagged"),
        ("draft_adds_citation", "AC-18-clean-texts-not-flagged"),
        ("draft_adds_citation", "AC-17-a-draft-01-cites-own-evidence"),
        ("flags_everything", "AC-18-clean-texts-not-flagged"),
    ],
)
def test_cases_fail_a_faulty_system(bug, test_id):
    results = run_against(FakeSystem({bug}), test_id[:5])
    assert results[test_id]["outcome"] == "fail", results[test_id]


def test_refused_moves_pass_whichever_amendmentpending_check_comes_first():
    # Without a filing, a system may reject the elements before the state; both are rejections.
    results = run_against(FakeSystem(elements_first=True), "AC-06")
    assert {r["outcome"] for r in results.values()} == {"pass"}, [
        r for r in results.values() if r["outcome"] != "pass"]
    assert "400 VALIDATION_ERROR on elements" in results["AC-06-UA-to-AmendmentPending"]["message"]


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
