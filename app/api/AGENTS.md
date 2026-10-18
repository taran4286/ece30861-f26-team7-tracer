# app/api/: the system's FastAPI app

The system's web layer. `server.py` mounts `system_app` at `/system`, so every path here sits under the system's base URL. Root "Directory guides" says which file wins when this one disagrees with the root `AGENTS.md` or `system-openapi.yaml`.

## Modules

| Module | Provides |
| --- | --- |
| `__init__.py` | `system_app`: the status page, `/health` and the `database_ok()` check both use, the `TracerError` handler, the three routers below, then the 501 catch-all |
| `pages.py` | The status page: `render()`, `operations()` from the spec, `STATUS` (how each spec operation is answered, the 501s included) and `shown_operations()`, the ones the page lists |
| `calls.py` | `identity()` (the header checks), `read_body()`, `call()` (headers, then the service in the thread pool) and `error_response()` (the Error schema) |
| `control.py` | The test-control routes, answering errors as `TestControlError` |
| `routes.py` | The project operations this team builds, one route per service operation |
| `stubs.py` | The 405 and 501 routes, through `services/placeholders.py` |

## What it serves

| Route | Response |
| --- | --- |
| `GET /` | 200 HTML status page: the title "Team 7 — Tracer system", whether the database is reachable, the spec's title and version linked to it, and the spec operations this system answers, marked built or 405 by design (the 501s aren't listed); `/system` without the slash redirects here with a 307 |
| `GET /health` | 200 `{"status": "ok", "database": "ok"}` after a `SELECT 1` through `get_engine()`; 503 `{"status": "unavailable", "database": "unavailable"}` when a setting is missing or invalid (such as no `DATABASE_URL` on a preview), the URL names a driver we don't install (a bare `postgresql://` scheme), or the database can't be reached |
| `POST /test/setup`, `GET /test/report`, `GET /test/report/{run_id}`, `PATCH /test/sessions/{session_id}/settings` | Root "Test control" |
| `POST /incidents`, `GET /incidents/{incident_id}`, evidence import, determination, transitions, `GET` audit | The services' results: 201 for create and import, 200 for the rest |
| The 405 and 501 operations | Root "Stubs" |
| Any other path and method pair, with GET, POST, PUT, PATCH or DELETE (such as `GET /incidents` or `POST /health`) | 501 `{"error": "NOT_IMPLEMENTED", "message": "/<path> is not implemented"}`, with no header or role checks |
| GET or HEAD on `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | FastAPI's generated docs for `system_app`; outside the spec |

Other methods, such as `HEAD` and `OPTIONS` elsewhere, get FastAPI's 405; no operation in the spec uses them.

## Rules

- Every project route goes through `calls.call()`. It checks `X-User-Id`, `X-User-Role`, then `X-Test-Run` (400 naming the first bad one) before any database access, then runs the service in FastAPI's thread pool, since services block on the database. The service checks the session and everything after it.
- Read bodies with `read_body()`, never a pydantic request model. A body that isn't JSON becomes `None`, which the service rejects at its body step, after the 404 checks; FastAPI's own 422 would come too early in the error order.
- A `TracerError` from a project route reaches the app's handler, which answers with `error_response()`: the Error schema with the status from `errors.HTTP_STATUS`. Leave `field` out when it has no value, since the schema types it as a string, not null.
- 405 responses carry an `Allow` header naming the methods the path does have; `stubs._refuse()` adds it.
- Test-control routes catch errors themselves and answer `{message, source: "system"}`: a `TracerError` with its status, unreachable storage with 500 "System storage is unavailable", and any other fault with 500 "System fault: …". Setup must still answer when the database is down.
- Register every route before the catch-all, which matches every path. `tests/test_server.py` checks that each project operation in `system-openapi.yaml` has its own route.
- `/health` isn't in the spec; it's the check that a deploy reaches the database (root "Deployment"). It logs the cause of a 503 as a warning and never puts it in the response.
- `GET /` isn't in the spec either: the status page shows in a browser that the deploy is up and what it answers. It still loads when the database can't be reached, showing it as unavailable, so it also works on previews. It reads `system-openapi.yaml` when first needed, not at import, so a missing file breaks only this page. `tester/` keeps its own page.
- Keep `pages.STATUS` in step with the routes. `tests/test_server.py` checks that it lists exactly the spec's operations and that each label matches what the route tests prove.
- Touch the database only inside a request, never at import: preview deployments have no `DATABASE_URL`, and the app must still start there.
- Never import `tester`; the two apps meet only in `server.py`.
