# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `conftest.py` | The `default_session` fixture, which stores `SESSION_ID` once per test run, and the `incident` fixture: a fresh UnderAssessment incident created by a Responder in that session |
| `helpers.py` | Acting users (`RESPONDER`, `APPROVER`, `BOARD`), all working in `SESSION_ID`, whose `test_now` is `TEST_NOW` and whose today is `TODAY` (2026-10-05); `new_session()` and `in_session()` for other sessions; `ticket()` and `alert()` item builders, `table_counts()`, `audit_entries()` and `set_state()` |
| `test_incidents.py` | `app/services/incidents.py`: create and get, session scoping and the session's today |
| `test_evidence.py` | `app/services/evidence.py`: import, normalization and all-or-nothing rejection |
| `test_workflow.py` | Determinations, all state targets from each state the workflow reaches, AmendmentPending's `info_available_date` and `elements` checks, date/error order, version handling, preservation, audit rollback and retrieval, the AC-09 materiality-write guardrail, and NUL-character rejection on create and import |
| `test_control.py` | `app/services/control.py`: setup defaults, settings and rejections, duplicate IDs, the coverage report and the snapshot's session scoping |
| `test_placeholders.py` | `app/services/placeholders.py`: 405 and 501 only after the session, role, incident and evidence checks, writing nothing |
| `test_server.py` | `server.py`'s landing page and mounts, the system's status page (its operations match the spec and the route tests), health check and catch-all; and the system's API over HTTP: a route for every spec operation, test control's responses and errors, project responses checked against the spec, error order on every route, 405s with `Allow`, and 501s |
| `test_tester.py` | The tester's page (how to run it, a stored report as tables, why a report is missing, escaping) and 501 stub; its session storage (one run per session, reports stored once); setup against `FakeSystem` (bad URLs, target errors passed on, no answer, each storing no session), a full run's stored report and coverage, the deadline abort, a missing coverage report, and runs whose planning or report check fails; `GET /test/report`; the tester and app roles' table access; and that `tester/` never imports `app` |
| `fake_system.py` | `FakeSystem`: an in-memory system following `system-openapi.yaml` as far as the cases and the runner need, coverage report included, with switchable `BUGS` |
| `test_tester_cases.py` | The tester's cases against `FakeSystem` under `/system`: all pass on a conforming system, each bug fails its case, errors get the right source, the clock check's window, and a stale `Date` header failing no case |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests can import `server` and `tests.helpers`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`; `test_health_reaches_the_database` fails when it isn't running. The tester's storage tests connect as `tracer_tester`, through `TESTER_DATABASE_URL`.
- FastAPI's `TestClient` needs `httpx2`, a project dependency because the tester uses it too.
- `test_tester_cases.py` needs no database: `FakeSystem` keeps its sessions in memory.

## Rules

- Neither the app role nor the tester role can delete, so nothing is cleaned up between tests. Assert on rows a test created itself (by incident or session ID), never on global contents; `table_counts()` is only for before-and-after comparisons around one call that must not write.
- A test that rejects a writing call checks the error code and the `field`, that `table_counts()` is unchanged and, for a call on an existing incident, that the incident reads back the same, since counts can't see changed rows.
- HTTP tests in `test_server.py` check bodies against the spec with `matches_spec()`, which uses the tester's `spec.response_errors`. That reports nothing for a status the spec doesn't list, so assert the status too.
- Header-error tests use the `no_database` fixture, which fails the test if `db.get_engine` or `db._session_factory` is called: header errors come before any database access. The catch-all tests use paths and methods no route handles.
- To put an incident straight into a state without the workflow, use `set_state()`; it changes only `state` and writes no audit entry.
- Every service operation needs a stored session, so each service test file starts with `pytestmark = pytest.mark.usefixtures("default_session")`; a new one does the same.
- Date checks compare against `TODAY`, or against the date of a session made with `new_session(test_now=...)`, never the real date.
- A test that needs other session settings, such as FPI, makes its own session with `new_session()` and acts in it through `in_session()`; never change a stored session. Make it before taking `table_counts()`, since it writes a row.
- A test that changes a setting or the environment clears the three cached objects before and after (see `app/AGENTS.md`), as the `fresh_settings` fixture in `test_server.py` does; `fresh_tester_settings` in `test_tester.py` does the same for `TESTER_DATABASE_URL`.
- pytest collects every function whose name starts with `test` and every class whose name starts with `Test` in a test file, imported ones included, and `tester_…` and `Tester…` names match. Import the module instead, e.g. `from tester import db as tester_db`, not `TesterSettings` or a `tester_…` alias.
- `FakeSystem` stands in for a target system, not for our service; never test `app` through it. When a case's expected answer changes, change `FakeSystem` to match, and when a case is added to catch a fault, add that fault to `BUGS` and a test that the case fails on it.
- The setup and run route tests point the runner at `FakeSystem` by patching `tester.runner.client_for`, and patch `tester.runner.planned` to a few cases when a test doesn't need the full suite. One module-scoped fixture, `finished`, does the only full run.
- Add a row to the table for each new test file.
