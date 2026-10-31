"""What the tester's setup and run routes do: prepare a target session, run the suite against
it, and build, check and store its report.

The target is reached only over HTTP, and each session lives in storage (sessions.py), so
setup, run and report lookup can each land on a different instance.
"""

import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx2

from tester.cases import planned
from tester.client import SETUP_TIMEOUT_SECONDS, Target, TargetError
from tester.sessions import ControlError, create_session, finish_run, start_run
from tester.spec import operation_tags, report_errors, schema_errors, tags
from tester.suite import Case, run_cases

# The pod's proposed limits (Tester API description): setup waits up to SETUP_TIMEOUT_SECONDS
# (20 s) for the target, and a run has 130 s, so it starts no new call after 105 s. That leaves
# room for the last call, the coverage request and storing the report.
CALL_CUTOFF_SECONDS = 105.0


def client_for(target_url: str) -> httpx2.Client:
    """An HTTP client for a target system's base URL. Tests replace it with a fake system."""
    return httpx2.Client(base_url=target_url)


def checked_target_url(value: Any) -> str:
    """A system base URL: HTTP(S), with no /test suffix, query, fragment or credentials."""
    if not isinstance(value, str) or not value.strip():
        raise ControlError(400, "target_url is required")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ControlError(400, "target_url must be an http or https URL")
    if parts.username is not None or parts.password is not None:
        raise ControlError(400, "target_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ControlError(400, "target_url must not have a query or fragment")
    if parts.path.rstrip("/").endswith("/test"):
        raise ControlError(400, "target_url is the system base URL, without /test")
    return value.rstrip("/")


def prepare(target_url: str) -> str:
    """Prepare a new session on the target and store it as ready; returns its ID (TEST-03).

    The target gets `setup: {}`, so its session uses the spec's default test_now, which the
    cases' dates assume. A target error is passed on with its status, message and source.
    """
    session_id = f"t7-{uuid.uuid4().hex}"
    try:
        with client_for(target_url) as http:
            response = http.post(
                "/test/setup",
                json={"session_id": session_id, "setup": {}},
                timeout=SETUP_TIMEOUT_SECONDS,
            )
    except httpx2.TimeoutException:
        raise ControlError(
            500, f"Target setup gave no response within {SETUP_TIMEOUT_SECONDS:g} s", "communication"
        )
    except httpx2.TransportError as exc:
        raise ControlError(500, f"Target setup failed: {type(exc).__name__}: {exc}", "communication")
    body = _json(response)
    if response.status_code != 201:
        raise _setup_failure(response.status_code, body)
    if not isinstance(body, dict) or body.get("session_id") != session_id:
        raise ControlError(500, "Target setup returned 201 without our session ID", "system")
    create_session(session_id, target_url)
    return session_id


def _json(response: httpx2.Response) -> Any:
    try:
        return response.json() if response.content else None
    except ValueError:
        return None


def _setup_failure(status: int, body: Any) -> ControlError:
    if status < 400:
        return ControlError(500, f"Target setup returned {status}, not 201", "system")
    if schema_errors("TestControlError", body):
        return ControlError(status, f"Target setup returned {status}", "system")
    return ControlError(status, body["message"], body["source"])


def run_session(session_id: str) -> dict[str, Any]:
    """Run every planned case against a prepared session and store its report (TEST-04).

    Returns the report of a finished run. Raises 404 or 409 before anything runs, and 500 once
    the run is aborted: by the deadline, by an invalid report, or by a fault in the tester. An
    aborted run's report is stored first.
    """
    # Planned before the run starts: planning reads the fixture files, and if that fails the
    # session stays ready instead of running.
    cases = planned()
    target_url = start_run(session_id)
    results: list[dict[str, Any]] = []
    try:
        with client_for(target_url) as http:
            target = Target(http, session_id, deadline=time.monotonic() + CALL_CUTOFF_SECONDS)
            results = run_cases(cases, target)
            report = {
                "session_id": session_id,
                "target_url": target_url,
                "results": results,
                "coverage": {
                    "code": code_coverage(target),
                    "component": component_coverage(target.called),
                    "system": system_coverage(results),
                },
            }
        problems = report_problems(report, cases)
    except Exception as exc:
        # A fault outside the cases, which run_cases already contains: store an aborted report
        # so the session doesn't stay running.
        unfinished = _unfinished_report(session_id, target_url, cases, results)
        finish_run(session_id, unfinished, aborted=True)
        raise ControlError(500, f"Tester fault: {type(exc).__name__}: {exc}") from exc
    finish_run(session_id, report, aborted=target.stopped or bool(problems))
    if problems:
        raise ControlError(500, f"The suite report is invalid, so the run is aborted: {problems[0]}")
    if target.stopped:
        raise ControlError(
            500, f"The run reached its {CALL_CUTOFF_SECONDS:g} s limit; its partial report is stored"
        )
    return report


def _unmeasured(reason: str) -> dict[str, Any]:
    return {"covered": None, "total": None, "basis": reason}


def code_coverage(target: Target) -> dict[str, Any]:
    """The target's own code coverage for the session, or null counts with the reason."""
    try:
        reply = target.coverage()
    except TargetError as exc:
        return _unmeasured(f"The target's coverage report gave no answer: {exc.message}")
    if reply.status != 200:
        return _unmeasured(f"The target's coverage report returned {reply.status}")
    if reply.schema_errors:
        return _unmeasured(f"The target's coverage report breaks the spec: {reply.schema_errors[0]}")
    measure = reply.body["coverage"]["code"]
    if measure["covered"] is not None and measure["covered"] > measure["total"]:
        return _unmeasured("The target's coverage report counts more covered than total")
    return {key: measure[key] for key in ("covered", "total", "basis")}


def component_coverage(called: set[tuple[str, str]]) -> dict[str, Any]:
    """The spec's components (its tags) that the run sent at least one request to."""
    reached = {tag for method, template in called for tag in operation_tags(method, template)}
    covered = [tag for tag in tags() if tag in reached]
    return {
        "covered": len(covered),
        "total": len(tags()),
        "basis": f"Spec tags the run sent a request to: {', '.join(covered) or 'none'}",
    }


def _criterion(test_id: str) -> str:
    # Test IDs start with their criterion: AC-06-DM-to-Closed is AC-06.
    return "-".join(test_id.split("-")[:2])


def system_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """The acceptance criteria with at least one case run, passed or not."""
    criteria = list(dict.fromkeys(_criterion(r["test_id"]) for r in results))
    ran = [c for c in criteria
           if any(_criterion(r["test_id"]) == c and r["outcome"] != "skip" for r in results)]
    return {
        "covered": len(ran),
        "total": len(criteria),
        "basis": f"Acceptance criteria with at least one case run: {', '.join(ran) or 'none'}",
    }


def report_problems(report: dict[str, Any], cases: list[Case]) -> list[str]:
    """Problems with a report: its SuiteReport schema, plus the rules the schema can't express."""
    problems = report_errors(report)
    test_ids = [result.get("test_id") for result in report["results"]]
    if len(set(test_ids)) != len(test_ids):
        problems.append("a test ID appears more than once")
    if set(test_ids) != {test_id for test_id, _ in cases}:
        problems.append("the results don't list every planned test")
    for name, measure in report["coverage"].items():
        if isinstance(measure.get("covered"), int) and measure["covered"] > measure["total"]:
            problems.append(f"{name} coverage counts more covered than total")
    return problems


def _unfinished_report(
    session_id: str, target_url: str, cases: list[Case], results: list[dict[str, Any]]
) -> dict[str, Any]:
    """An aborted report: the cases' results if they all ran, otherwise every case skipped."""
    reason = "the tester failed before the run finished"
    skipped = [{"test_id": test_id, "outcome": "skip", "message": f"Not run: {reason}"}
               for test_id, _ in cases]
    unmeasured = _unmeasured(f"Not measured: {reason}")
    return {
        "session_id": session_id,
        "target_url": target_url,
        "results": results or skipped,
        "coverage": {"code": unmeasured, "component": unmeasured, "system": unmeasured},
    }
