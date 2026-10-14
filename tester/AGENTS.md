# tester/: our tester

Our tester service from the shared Tester API; group A owns it. `server.py` mounts `tester_app` at `/tester`, its own base URL. The root `AGENTS.md` wins wherever this file disagrees.

## What it serves

- Every path apart from the docs pages below returns 501 for GET, POST, PUT, PATCH and DELETE, with `{"message": …, "source": "tester"}`, the test-control error shape.
- FastAPI's generated `/docs`, `/redoc` and `/openapi.json` also answer, and other methods such as `HEAD` and `OPTIONS` get FastAPI's 405; both are outside the spec.

## Rules

- Never import `app`. The tester checks systems, ours included, only through their HTTP API; `tests/test_tester.py` fails on any `app` import here.
- Test-control errors carry `message` and `source`, never the project `Error` schema.
