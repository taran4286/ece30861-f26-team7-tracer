# app/api/: the system's FastAPI app

The system's web layer; group A owns it. `server.py` mounts `system_app` at `/system`, so every path here sits under the system's base URL. The root `AGENTS.md` wins wherever this file disagrees.

## What it serves

| Route | Response |
| --- | --- |
| `GET /health` | 200 `{"status": "ok", "database": "ok"}` after a `SELECT 1` through `get_engine()`; 503 with `"database": "unavailable"` when a setting is missing or invalid (such as no `DATABASE_URL` on a preview), the URL names a driver we don't install (a bare `postgresql://` scheme), or the database can't be reached |
| Any other path, with GET, POST, PUT, PATCH or DELETE | 501 `{"error": "NOT_IMPLEMENTED", "message": …}` |
| `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | FastAPI's generated docs for the stub; outside the spec |

Other methods, such as `HEAD` and `OPTIONS`, get FastAPI's 405; no operation in the spec uses them.

## Rules

- `/health` isn't in the spec; it's the check that a deploy reaches the database (root "Deployment"). It logs the cause of a 503 as a warning and never puts it in the response.
- Leave `field` out of an error body when it has no value: the Error schema types it as a string, not null. `TracerError.to_dict()` still returns `field: None`, so drop that key before sending its output.
- The 501 catch-all matches every path, so declare every other route before it.
- Touch the database only inside a request, never at import: preview deployments have no `DATABASE_URL`, and the app must still start there.
- Never import `tester`; the two apps meet only in `server.py`.
