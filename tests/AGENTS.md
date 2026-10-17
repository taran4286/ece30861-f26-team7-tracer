# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `conftest.py` | The `incident` fixture: a fresh UnderAssessment incident created by a Responder |
| `helpers.py` | Acting users (`RESPONDER`, `APPROVER`, `BOARD`), `ticket()` and `alert()` item builders, `table_counts()`, `audit_entries()` and `set_state()` |
| `test_incidents.py` | `services/incidents.py`: create and get |
| `test_evidence.py` | `services/evidence.py`: import, normalization and all-or-nothing rejection |
| `test_workflow.py` | Determination validation, forms, audit atomicity and the AC-09 materiality-write guardrail |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests import `tests.helpers`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`.

## Rules

- The app role can't delete, so nothing is cleaned up between tests. Assert on rows a test created itself (by incident ID), never on global contents; `table_counts()` is only for before-and-after comparisons around one rejected call.
- Every rejection test checks the error code, the `field`, and that `table_counts()` is unchanged.
- To put an incident in a state the workflow can't reach yet, use `set_state()`; it writes no audit entry.
- Add a row to the table for each new test file.
