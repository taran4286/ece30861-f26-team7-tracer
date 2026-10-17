# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `conftest.py` | The `incident` fixture: a fresh UnderAssessment incident created by a Responder |
| `helpers.py` | Acting users (`RESPONDER`, `APPROVER`, `BOARD`), `ticket()` and `alert()` item builders, `table_counts()`, `audit_entries()` and `set_state()` |
| `test_incidents.py` | `app/services/incidents.py`: create and get |
| `test_evidence.py` | `app/services/evidence.py`: import, normalization and all-or-nothing rejection |
| `test_workflow.py` | Determinations, all state targets from Phase 3 states, date/error order, version handling, preservation, audit rollback and retrieval, the AC-09 materiality-write guardrail, and NUL-character rejection on create and import |
| `test_server.py` | `server.py`'s mounts, captioned photo, health check, catch-all, and explicit audit 405 / standalone 501 responses with header, role and no-database checks |
| `test_tester.py` | The tester's captioned photo page and 501 stub, and that `tester/` never imports `app` |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests can import `server` and `tests.helpers`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`; `test_health_reaches_the_database` fails when it isn't running.
- FastAPI's `TestClient` needs `httpx2`, which the `dev` extra installs.

## Rules

- The app role can't delete, so nothing is cleaned up between tests. Assert on rows a test created itself (by incident ID), never on global contents; `table_counts()` is only for before-and-after comparisons around one call that must not write.
- A test that rejects a writing call checks the error code and the `field`, that `table_counts()` is unchanged and, for a call on an existing incident, that the incident reads back the same, since counts can't see changed rows.
- The placeholder route tests instead use the `no_database` fixture, which fails the test if `db.get_engine` or `db._session_factory` is called. The generic catch-all tests exclude the explicit audit 405 methods.
- To put an incident straight into a state without the workflow, use `set_state()`; it changes only `state` and writes no audit entry.
- A test that changes a setting or the environment clears the three cached objects before and after (see `app/AGENTS.md`): the `fresh_settings` fixture in `test_server.py`, `clear_caches()` in `test_workflow.py`.
- Add a row to the table for each new test file.
