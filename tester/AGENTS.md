# tester/: our tester

Our tester service from the shared Tester API; group A owns it. `server.py` mounts `tester_app` at `/tester`, its own base URL. The root `AGENTS.md` wins wherever this file disagrees.

## What it serves

- `GET /` returns an HTML page with `tester.png` inlined and captioned "Team 7 tester", so a browser shows the deploy is up; `/tester` without the slash redirects there with a 307. It's outside the spec, and reads the photo per request, so a missing file breaks only this page.
- Every other path apart from the docs pages below returns 501 for GET, POST, PUT, PATCH and DELETE, with `{"message": …, "source": "tester"}`, the test-control error shape. That includes `/test/setup`, `/test/run` and `/test/report`: the cases below exist, but no route runs them yet.
- FastAPI's generated `/docs`, `/docs/oauth2-redirect`, `/redoc` and `/openapi.json` also answer, and other methods such as `HEAD` and `OPTIONS` get FastAPI's 405; both are outside the spec.

## Cross-team test cases

`cases.run_suite(target)` runs every planned case, in order, against one prepared session of a target system and returns one `TestResult` per case.

| Module | Does |
| --- | --- |
| `client.py` | `Target`: sends a call to the target's base URL with `X-Test-Run`, `X-User-Id` and `X-User-Role` and a 10 s timeout, no retry, and checks the reply against the spec; `Actor` holds the identity headers |
| `spec.py` | Loads `system-openapi.yaml` from the repo root and checks JSON against its schemas, `date` and `date-time` formats included |
| `suite.py` | `run_cases()`, the outcome rules, `expect()`, and `Run`: shared setups (`once`, `forget`) and the snapshot baseline for "stored data unchanged" checks |
| `cases.py` | The cases, by criterion: AC-02, 03, 07, 08, 20, 06, 09 (a), 23 (a)–(c), then 01, each with normal cases and edge cases |

- Outcomes: `pass` when the reply meets the pass rule, an expected 403, 404 or 409 included. `fail` when it doesn't, including a body that breaks its spec schema and a 501 on a route the case needs. `error` with `source: system` for any other 5xx, `communication` when no response arrives, and `tester` for a fault in our code. `skip` for a case not run.
- A shared setup that fails, such as the incident several cases use, gives each of those cases the same outcome, its message starting `Setup failed:`.
- "Stored data unchanged" compares the target's snapshot (`GET /test/report/{run_id}`) before and after the call, with arrays sorted by ID.
- AC-06 plans all 49 state pairs and runs the 33 Phase 3 can reach; the 16 that need a filing are `skip`. A source state's allowed move runs after its refused ones, and a case that sees the incident leave its state calls `run.forget`, so later cases get a new one.
- AC-01 runs last, so its 100 incidents don't enlarge every earlier snapshot.

## Rules

- Never import `app`. The tester checks systems, ours included, only through their HTTP API; `tests/test_tester.py` fails on any `app` import here.
- Test-control errors carry `message` and `source`, never the project `Error` schema.
- The 501 catch-all matches every path, so declare every other route before it.
- Expected statuses, error codes and fields come from `system-openapi.yaml`, and each case's pass rule from its course acceptance criterion; where the tester plan disagrees with the spec, the spec wins. For example, Responder and Board get 403 on the audit and delete routes, and only a DisclosureApprover gets 405.
- Check only what the spec states. It sets no order between errors, so each rejection case's request has one thing wrong with it; due dates and flags are Phase 4, so no case checks them.
- Dates in the cases assume the session's `test_now` is the spec's default, 2026-10-05T15:00:00Z (`TODAY` in `cases.py`), so the runner's setup must send `setup: {}`.
- Make every project call through `Run.call`, never `Target.call`: it ends the saved snapshot baseline, so a rejection is never compared with stale data.
- Test IDs are stable and unique, and start with their criterion (`AC-06-DM-to-Closed`); reports from both teams are compared by them.
- `spec.py` and the cases read `system-openapi.yaml` and `fixtures/` when first needed, so a missing file fails only the run, not the app.
