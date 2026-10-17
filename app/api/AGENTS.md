# app/api/: the system's FastAPI app

The system's web layer. `server.py` mounts `system_app` at `/system`, so every path here sits under the system's base URL. Root "Directory guides" says which file wins when this one disagrees with the root `AGENTS.md` or `system-openapi.yaml`.

## What it serves

| Route | Response |
| --- | --- |
| `GET /` | 200 HTML page: `system.png`, inlined, captioned "Team 7 service"; `/system` without the slash redirects here with a 307 |
| `GET /health` | 200 `{"status": "ok", "database": "ok"}` after a `SELECT 1` through `get_engine()`; 503 `{"status": "unavailable", "database": "unavailable"}` when a setting is missing or invalid (such as no `DATABASE_URL` on a preview), the URL names a driver we don't install (a bare `postgresql://` scheme), or the database can't be reached |
| `PUT`, `PATCH`, `DELETE /incidents/{incident_id}/audit` | Header checks, then 403 for Responder and Board or 405 `METHOD_NOT_ALLOWED` for DisclosureApprover |
| `PATCH /incidents/{incident_id}/evidence/{evidence_id}`, `POST /groups` | Header checks, then 403 for Board or 501 `NOT_IMPLEMENTED` for Responder and DisclosureApprover |
| Any other path and method pair not handled by an earlier route, with GET, POST, PUT, PATCH or DELETE (such as `POST /health` or `GET /groups`) | 501 `{"error": "NOT_IMPLEMENTED", "message": "/<path> is not implemented"}`, with no header or role checks |
| GET or HEAD on `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | FastAPI's generated docs for `system_app`; outside the spec |

Other methods, such as `HEAD` and `OPTIONS` elsewhere, get FastAPI's 405; no operation in the spec uses them.

## Rules

- `stubs.py` holds the explicit placeholder router, included before the catch-all. It validates `X-User-Id`, `X-User-Role`, then `X-Test-Run` manually, returning 400 with the header name as `field`, before checking the shared role permissions. It never reads request bodies or accesses the database. These placeholders don't look up path IDs (no 404) or check that the `X-Test-Run` session exists.
- `/health` isn't in the spec; it's the check that a deploy reaches the database (root "Deployment"). It logs the cause of a 503 as a warning and never puts it in the response.
- `GET /` isn't in the spec either: its captioned photo shows in a browser that the deploy is up. It never touches the database, so it also works on previews. It reads `system.png` per request, not at import, so a missing file breaks only this page. `tester/` keeps its own photo and page.
- Leave `field` out of an error body when it has no value: the Error schema types it as a string, not null. `TracerError.to_dict()` still returns `field: None`, so drop that key before sending its output.
- The 501 catch-all matches every path, so declare every other route before it.
- Touch the database only inside a request, never at import: preview deployments have no `DATABASE_URL`, and the app must still start there.
- Never import `tester`; the two apps meet only in `server.py`.
