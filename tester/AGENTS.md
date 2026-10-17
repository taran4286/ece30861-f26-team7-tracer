# tester/: our tester

Our tester service, implementing `tester-openapi.yaml`. `server.py` mounts `tester_app` at `/tester`, its own base URL. Root "Directory guides" says which file wins when this one disagrees with the root `AGENTS.md` or `tester-openapi.yaml`.

## What it serves

- `GET /` returns an HTML page with `tester.png` inlined and captioned "Team 7 tester", so a browser shows the deploy is up; `/tester` without the slash redirects there with a 307. It's outside the spec, and reads the photo per request, so a missing file breaks only this page.
- Any other GET, POST, PUT, PATCH or DELETE request, including the spec's `/test/setup`, `/test/run` and `/test/report`, returns 501 `{"message": "/<path> is not implemented", "source": "tester"}`, the spec's `TestControlError` shape.
- GET and HEAD on FastAPI's generated `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` also answer, and other methods such as `HEAD` and `OPTIONS` elsewhere get FastAPI's 405; both are outside the spec.

## Rules

- Never import `app`. The tester checks systems, ours included, only through their HTTP API; `tests/test_tester.py` fails on any `app` import here.
- Test-control errors carry `message` and `source`, never the project `Error` schema.
- The 501 catch-all matches every path, so declare every other route before it.
