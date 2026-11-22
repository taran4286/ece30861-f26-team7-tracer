"""Self-only design coverage, exact traceability, conservative counts and stored reports."""

import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import app as server_app
from tester import config, db, model_coverage, runner
from tester.cases import RESPONDER, planned
from tester.spec import report_errors
from tester.suite import Fail, expect
from tests.fake_system import FakeSystem

OWN = "http://local/system"
OTHER = "https://tracer-team10.vercel.app/system"
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


def results(outcome="pass", message="Evaluated"):
    return [{"test_id": tid, "outcome": outcome, "message": message} for tid, _ in planned()]


def test_mapping_names_real_tests_and_retains_unmeasured_design_requirements():
    model_coverage.validate_mapping([tid for tid, _ in planned()])
    rows = model_coverage.requirements()
    assert len(rows) == len({r.id for r in rows}) == 14
    assert {r.id for r in rows if not r.test_ids} == {
        "P5-D08", "P5-D13",
    }
    data = json.loads(model_coverage.MAPPING_PATH.read_text(encoding="utf-8"))
    assert all(r.get("evaluation_gap") for r in data["requirements"] if not r["test_ids"])


@pytest.mark.parametrize("outcome", ["pass", "fail"])
def test_all_evaluated_checks_cover_twelve_requirements_even_when_they_fail(outcome):
    measure = model_coverage.measure(results(outcome))
    assert (measure["covered"], measure["total"]) == (12, 14)
    assert model_coverage.DESIGN_VERSION in measure["basis"]
    assert model_coverage.MAPPING_URL in measure["basis"]


@pytest.mark.parametrize("outcome,message", [
    ("skip", "Not run"), ("error", "No response"),
    ("fail", "Setup failed: no incident"), ("fail", "Before this case, reset failed"),
])
def test_checks_prevented_from_evaluation_do_not_count(outcome, message):
    assert model_coverage.measure(results(outcome, message))["covered"] == 0


def test_partial_requirement_needs_every_mapped_check(monkeypatch):
    rows = (model_coverage.DesignRequirement("D1", ("a", "b")),
            model_coverage.DesignRequirement("D2", ()))
    monkeypatch.setattr(model_coverage, "requirements", lambda: rows)
    partial = model_coverage.measure([{"test_id": "a", "outcome": "pass", "message": "ok"}])
    assert (partial["covered"], partial["total"]) == (0, 2)
    assert model_coverage.measure([
        {"test_id": "a", "outcome": "pass", "message": "ok"},
        {"test_id": "b", "outcome": "fail", "message": "wrong value"},
    ])["covered"] == 1


def test_mapping_drift_is_rejected(monkeypatch):
    row = replace(model_coverage.requirements()[0], test_ids=("renamed-or-removed",))
    monkeypatch.setattr(model_coverage, "requirements", lambda: (row,))
    with pytest.raises(ValueError, match="unplanned tests"):
        model_coverage.validate_mapping([tid for tid, _ in planned()])


@pytest.mark.parametrize("target,own,expected", [
    (OWN, OWN, True), (OWN+"/", OWN, True),
    ("https://EXAMPLE.com:443/system/", "https://example.com/system", True),
    (OTHER, OWN, False), (OWN+"-other", OWN, False),
    ("http://local:81/system", OWN, False), ("https://local/system", OWN, False),
    ("http://local:0/system", OWN, False),
    ("http://local.example/system", OWN, False),
    (OWN+"?other=1", OWN, False), ("http://user@local/system", OWN, False),
    ("not a URL", OWN, False),
])
def test_only_the_configured_complete_system_url_is_a_self_test(target, own, expected):
    assert model_coverage.is_own_system(target, own) is expected


def test_own_system_setting_defaults_to_production_and_allows_a_local_override():
    settings = config.TesterSettings(_env_file=None, tester_database_url="unused")
    assert settings.tester_own_system_url == config.DEFAULT_OWN_SYSTEM_URL
    assert config.TesterSettings(_env_file=None, tester_database_url="unused",
                                tester_own_system_url=OWN+"/").tester_own_system_url == OWN
    with pytest.raises(ValidationError):
        config.TesterSettings(_env_file=None, tester_database_url="unused",
                              tester_own_system_url="https://user:secret@host/system")


def report(target=OWN):
    empty = {"covered": 0, "total": 0, "basis": "No checks"}
    return {"session_id": "s", "target_url": target, "results": [],
            "coverage": {k: dict(empty) for k in ("code", "component", "system")}}


@pytest.mark.parametrize("measure", [
    {"covered": 1, "total": None, "basis": "mixed"},
    {"covered": -1, "total": 14, "basis": "negative"},
    {"covered": True, "total": 14, "basis": "boolean"},
    {"covered": 1, "total": 14, "basis": ""},
    {"covered": 1, "total": 14},
])
def test_local_schema_validation_rejects_invalid_model_measures(measure):
    data = report()
    data["coverage"]["model"] = measure
    assert any("coverage.model" in error for error in report_errors(data))


def test_report_validation_requires_model_only_for_self_and_checks_overcount():
    own = report()
    assert "a self-test report must include model coverage" in runner.report_problems(
        own, [], own_system_url=OWN)
    own["coverage"]["model"] = {"covered": 15, "total": 14, "basis": "Too many"}
    assert "model coverage counts more covered than total" in runner.report_problems(
        own, [], own_system_url=OWN)
    own["target_url"] = OTHER
    assert "a cross-team report must omit model coverage" in runner.report_problems(
        own, [], own_system_url=OWN)
    del own["coverage"]["model"]
    assert runner.report_problems(own, [], own_system_url=OWN) == []


@pytest.fixture
def small_self_suite(monkeypatch):
    """An actual tester run/report transaction with two cheap, controlled target checks."""
    def clear():
        config.get_tester_settings.cache_clear()
        db.get_engine.cache_clear()
        db._session_factory.cache_clear()

    clear()
    monkeypatch.setenv("TESTER_OWN_SYSTEM_URL", OWN)
    target = FastAPI()
    target.mount("/system", FakeSystem().app)
    monkeypatch.setattr(runner, "client_for", lambda url: TestClient(target, base_url=url))

    def first(run):
        expect(run.call("POST", "/incidents", RESPONDER,
                        json={"title": "Local", "discovery_date": "2026-10-01"}), 201)
        return "Evaluated"

    def mismatch(run):
        raise Fail("Deliberate evaluated mismatch")

    cases = [("AC-01-first", first), ("AC-01-second", mismatch)]
    monkeypatch.setattr(runner, "planned", lambda: cases)
    monkeypatch.setattr(model_coverage, "requirements", lambda: (
        model_coverage.DesignRequirement("D1", tuple(tid for tid, _ in cases)),))
    try:
        with TestClient(server_app) as client:
            yield client
    finally:
        clear()


def start_and_run(client, target=OWN):
    response = client.post("/tester/test/setup", json={"target_url": target})
    assert response.status_code == 201, response.text
    sid = response.json()["session_id"]
    return sid, client.post("/tester/test/run", json={"session_id": sid})


def test_self_report_is_stored_displayed_and_not_recomputed(small_self_suite, monkeypatch):
    client = small_self_suite
    sid, response = start_and_run(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["outcome"] for r in body["results"]] == ["pass", "fail"]
    assert body["coverage"]["model"]["covered"] == 1
    assert report_errors(body) == []
    # A later mapping change must not rewrite a finalized stored report.
    monkeypatch.setattr(model_coverage, "measure", lambda results: pytest.fail("Recomputed"))
    assert client.get("/tester/test/report", params={"session_id": sid}).json() == body
    assert "<td>model</td>" in client.get("/tester/", params={"session_id": sid}).text


def test_cross_report_omits_model_without_loading_our_mapping(small_self_suite, monkeypatch):
    monkeypatch.setattr(model_coverage, "requirements", lambda: pytest.fail("Cross-team mapping"))
    _, response = start_and_run(small_self_suite, OTHER)
    assert response.status_code == 200, response.text
    assert "model" not in response.json()["coverage"]


def test_deadline_abort_keeps_partial_self_coverage(small_self_suite, monkeypatch):
    monkeypatch.setattr(runner, "CALL_CUTOFF_SECONDS", 0)
    sid, response = start_and_run(small_self_suite)
    assert response.status_code == 500
    body = small_self_suite.get("/tester/test/report", params={"session_id": sid}).json()
    assert [r["outcome"] for r in body["results"]] == ["error", "skip"]
    assert body["coverage"]["model"]["covered"] == 0
    assert body["coverage"]["model"]["total"] == 1


def test_mapping_fault_keeps_self_report_with_null_measure(small_self_suite, monkeypatch):
    def broken(ids):
        raise ValueError("Unplanned mapping ID")

    monkeypatch.setattr(model_coverage, "validate_mapping", broken)
    sid, response = start_and_run(small_self_suite)
    assert response.status_code == 500
    body = small_self_suite.get("/tester/test/report", params={"session_id": sid}).json()
    model = body["coverage"]["model"]
    assert (model["covered"], model["total"]) == (None, None)
    assert model_coverage.DESIGN_VERSION in model["basis"]
    assert report_errors(body) == []
