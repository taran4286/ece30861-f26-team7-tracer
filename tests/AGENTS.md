# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `conftest.py` | The `incident` fixture: a fresh UnderAssessment incident created by a Responder |
| `helpers.py` | Acting users (`RESPONDER`, `APPROVER`, `BOARD`), `ticket()` and `alert()` item builders, `table_counts()`, `audit_entries()` and `set_state()` |
| `test_incidents.py` | `services/incidents.py`: create and get |
| `test_evidence.py` | `services/evidence.py`: import, normalization and all-or-nothing rejection |
| `test_workflow.py` | Determinations, all state targets from Phase 3 states, date/error order, version handling, preservation, audit rollback and retrieval, and the AC-09 materiality-write guardrail |
| `test_server.py` | `server.py`'s mounts, captioned photo, health check, catch-all, and explicit audit 405 / standalone 501 responses with header, role and no-database checks |
| `test_tester.py` | The tester's captioned photo page and 501 stub, its session storage (one run per session, reports stored once) and `GET /test/report`, the tester and app roles' table access, and that `tester/` never imports `app` |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests can import `server` and `tests.helpers`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`; `test_health_reaches_the_database` fails when it isn't running. The tester's storage tests connect as `tracer_tester`, through `TESTER_DATABASE_URL`.
- FastAPI's `TestClient` needs `httpx2`, which the `dev` extra installs.

## Rules

- Neither the app role nor the tester role can delete, so nothing is cleaned up between tests. Assert on rows a test created itself (by incident or session ID), never on global contents; `table_counts()` is only for before-and-after comparisons around one rejected call.
- Every rejection test checks the error code, the `field`, and that `table_counts()` is unchanged.
- The placeholder route tests instead block the engine and session factory, proving those requests cannot reach the database. The generic catch-all tests exclude the explicit audit 405 methods.
- To put an incident in a state the workflow can't reach yet, use `set_state()`; it writes no audit entry.
- A test that changes a setting or the environment clears the three cached objects before and after (see `app/AGENTS.md`); in `test_server.py`, the `fresh_settings` fixture does it.
- pytest collects every function whose name starts with `test` and every class whose name starts with `Test` in a test file, imported ones included, and `tester_…` and `Tester…` names match. Import the module instead, e.g. `from tester import db as tester_db`, not `TesterSettings` or a `tester_…` alias.
- Add a row to the table for each new test file.
