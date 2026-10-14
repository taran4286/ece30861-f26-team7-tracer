# tests/: the pytest suite

The root "Tests to write" section lists what each area must cover; this file covers how the suite runs.

| File | Covers |
| --- | --- |
| `test_server.py` | `server.py`'s mounts, the system's `/health` check and its 501 stub |
| `test_tester.py` | The tester's 501 stub, and that `tester/` never imports `app` |

## Running

- Run `pytest` from the repo root after local setup steps 1 to 5. `pyproject.toml` puts the root on the import path, so tests can import `server`.
- Tests use the real local database as `tracer_app`, through `DATABASE_URL` in `.env`; `test_health_reaches_the_database` fails when it isn't running.
- FastAPI's `TestClient` needs `httpx2`, which the `dev` extra installs.

## Rules

- A test that changes a setting or the environment clears `get_settings` and `get_engine` before and after (see `app/AGENTS.md`).
- Add a row to the table for each new test file.
