# app/api/: the system's FastAPI app

The system's web layer. `server.py` mounts `system_app` at `/system`, so every path here sits under the system's base URL. What each operation does is in `services/AGENTS.md`; this file covers routing, headers and response shapes.

## Modules

| Module | Provides |
| --- | --- |
| `__init__.py` | `system_app`: the status page, `/health` and the `database_ok()` check both use, the `TracerError` handler, the four routers below, then the 501 catch-all |
| `pages.py` | The status page: `render()`, `operations()` read from `system-openapi.yaml`, `STATUS` (how each operation in that file is answered, the 501s included) and `shown_operations()`, the ones the page lists. `SPEC_LINK` points at the file on GitHub's `main`, not the deployed copy |
| `dashboard.py` | The dashboard: its route, `NOTICES` (each flag's text and color, most urgent first) and the HTML, built from `control.snapshot` |
| `calls.py` | `identity()` (the header checks), `read_body()`, `call()` (headers, then the service in the thread pool) and `error_response()` (the Error body) |
| `control.py` | The test-control routes, answering errors as `{message, source}` |
| `routes.py` | The project operations, one route per service operation |
| `stubs.py` | The 405 and 501 routes, through `services/placeholders.py` |

## What it serves

| Route | Response |
| --- | --- |
| `GET /` | 200 HTML status page: the title "Team 7 — Tracer system", whether the database is reachable, the system API's title and version linked to `SPEC_LINK`, and a link to the dashboard, and the operations this system answers, marked built or 405 by design (the 501s aren't listed); `/system` without the slash redirects here with a 307 |
| `GET /dashboard?session_id=` | 200 HTML dashboard: the session's test date with a form to change it, its registrant type and at-risk threshold, and its incidents, most urgent first, with state, required form, due date, notices, filings and version. No `session_id` gives only the session form; an unknown session is a 404 page and an unreachable database a 503 page |
| `GET /health` | 200 `{"status": "ok", "database": "ok"}` after a `SELECT 1` through `get_engine()`; 503 `{"status": "unavailable", "database": "unavailable"}` when a setting is missing or invalid (such as no `DATABASE_URL` on a preview), the URL names a driver we don't install (a bare `postgresql://` scheme), or the database can't be reached |
| `POST /test/setup` | 201 `{session_id}` from `control.setup_session` |
| `GET /test/report?session_id=` | 200 coverage from `control.coverage_report` |
| `GET /test/report/{run_id}` | 200 snapshot from `control.snapshot` |
| `PATCH /test/sessions/{session_id}/settings` | 200 TestRun from `control.update_settings`, given the path ID and `read_body()`'s result |
| `POST /incidents`, `GET /incidents/{incident_id}`, `POST …/evidence/import`, `POST …/determination`, `POST …/transitions`, `GET …/audit` | The service's result: 201 for create and import, 200 for the rest |
| `POST /incidents/{incident_id}/filings` | 201 full Incident from `filings.record_filing`, through the same header and body handling as other project routes |
| The 405 and 501 operations (`services/AGENTS.md`, "405 and 501 operations") | The service's error; each 405 carries `Allow`: `GET` for the incident and audit paths, `PATCH` for the evidence path |
| Any other path and method pair, with GET, POST, PUT, PATCH or DELETE (such as `GET /incidents` or `POST /health`) | 501 `{"error": "NOT_IMPLEMENTED", "message": "/<path> is not implemented"}`, with no header or role checks |
| GET or HEAD on `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | FastAPI's generated docs for `system_app` |

`HEAD` and `OPTIONS` on every other path, `/` and `/health` included, get FastAPI's 405. Its `Allow` header names only the first matching route's methods (`HEAD /incidents/x` gives `Allow: GET`), so it doesn't list everything the path answers.

## Rules

- Every project route goes through `calls.call()`. It checks the identity headers before any database access, then runs the service in FastAPI's thread pool, since services block on the database. The service checks the session and everything after it.
- `identity()` checks `X-User-Id`, `X-User-Role`, then `X-Test-Run`, and answers the first bad one with 400 `VALIDATION_ERROR` and `field` set to the header name. `X-User-Id` and `X-Test-Run` must be present and not blank (a whitespace-only value is a 400, while a value with surrounding spaces is passed on unchanged); `X-User-Role` must equal a `Role` value exactly, case included. Test-control routes take no identity headers.
- Read bodies with `read_body()`, never a pydantic request model. A body that isn't JSON (or no body) becomes `None`, which the service rejects at its body step (in a project operation, after the 404 checks); FastAPI's own 422 would come too early in the error order.
- A `TracerError` from a project route reaches the app's handler, which answers with `error_response()`: `{error, message, field}` with the status from `errors.HTTP_STATUS`, and `field` left out when it has no value.
- 405 responses carry an `Allow` header naming the methods the path does have; `stubs._refuse()` adds it.
- Test-control routes catch errors themselves in `control._respond()` and answer `{message, source: "system"}`: a `TracerError` with its status, unreachable storage (a missing or invalid setting, a driver we don't install, a malformed URL, or no database) with 500 "System storage is unavailable", and any other fault with 500 "System fault: <exception name>". Setup's and the settings update's 400s still answer when the database is down, since those services validate first.
- Register every route before the catch-all, which matches every path. `tests/test_server.py` checks that each project operation in `system-openapi.yaml` has its own route.
- `/health` is the check that a deploy reaches the database (root "Deployment"). It logs the cause of a 503 as a warning and never puts it in the response.
- The status page shows in a browser that the deploy is up and what it answers. It still loads when the database can't be reached, showing it as unavailable, so it also works on previews. It reads `system-openapi.yaml` on first use, not at import, and caches it for the life of the process, so a missing file breaks only this page. `tester/` keeps its own page.
- The dashboard takes no identity headers and only reads. Its date form sends `test_now` to `PATCH /test/sessions/{session_id}/settings` from the browser, through a path relative to the page, keeping the time of day, and reloads on success; any other answer shows the error's `message` on the page. Everything shown from storage or the query string is escaped. Below 640 px each incident stacks into a block, so its notices stay in view.
- Keep `pages.STATUS` in step with the routes. `tests/test_server.py` checks that it lists exactly the operations in `system-openapi.yaml` and that each label matches what the route tests prove.
- Touch the database only inside a request, never at import: preview deployments have no `DATABASE_URL`, and the app must still start there.
- Never import `tester`; the two apps meet only in `server.py`.
