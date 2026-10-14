# app/api/: the system's FastAPI app

The system's web layer; group A owns it. `server.py` mounts `system_app` at `/system`, so every path here sits under the system's base URL. The root `AGENTS.md` wins wherever this file disagrees.

## What it serves

| Route | Response |
| --- | --- |
| `GET /health` | 200 `{"status": "ok", "database": "ok"}` after a `SELECT 1` through `get_engine()`; 503 with `"database": "unavailable"` when `DATABASE_URL` is unset or the query fails |
| Any other path, any method | 501 `{"error": "NOT_IMPLEMENTED", "message": …}` |

## Rules

- `/health` isn't in the spec; it's the check that a deploy reaches the database (root "Deployment"). It logs the cause of a 503 as a warning and never puts it in the response.
- Leave `field` out of an error body when it has no value: the Error schema types it as a string, not null.
- The 501 catch-all matches every path, so declare every other route before it.
- Touch the database only inside a request, never at import: preview deployments have no `DATABASE_URL`, and the app must still start there.
- Never import `tester`; the two apps meet only in `server.py`.
