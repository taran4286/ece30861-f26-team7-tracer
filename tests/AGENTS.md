# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `conftest.py` | The `incident` fixture: a fresh UnderAssessment incident created by a Responder |
| `helpers.py` | Acting users (`RESPONDER`, `APPROVER`, `BOARD`), `ticket()` and `alert()` item builders, `table_counts()`, `audit_entries()` and `set_state()` |
| `test_incidents.py` | `services/incidents.py`: create and get |
| `test_evidence.py` | `services/evidence.py`: import, normalization and all-or-nothing rejection |
| `test_server.py` | `server.py`'s mounts, the system's photo, `/health` check and 501 stub |
| `test_tester.py` | The tester's photo and 501 stub, and that `tester/` never imports `app` |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests can import `server` and `tests.helpers`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`; `test_health_reaches_the_database` fails when it isn't running.
- FastAPI's `TestClient` needs `httpx2`, which the `dev` extra installs.

## Rules

- The app role can't delete, so nothing is cleaned up between tests. Assert on rows a test created itself (by incident ID), never on global contents; `table_counts()` is only for before-and-after comparisons around one rejected call.
- Every rejection test checks the error code, the `field`, and that `table_counts()` is unchanged.
- To put an incident in a state the workflow can't reach yet, use `set_state()`; it writes no audit entry.
- A test that changes a setting or the environment clears the three cached objects before and after (see `app/AGENTS.md`); in `test_server.py`, the `fresh_settings` fixture does it.
- Add a row to the table for each new test file.
