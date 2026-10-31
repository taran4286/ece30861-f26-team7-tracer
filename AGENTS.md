# Tracer — Team 7 Implementation

## Directory guides

The `AGENTS.md` files describe the code as it is, so an agent can rely on them instead of re-reading it. The code is the truth: where a guide disagrees with it, fix the guide. This root file holds the repo-wide rules; what each folder's code does is in that folder's own guide.

- Before changing or explaining a file, read the `AGENTS.md` in its folder and in each parent folder up to the root. Not every tool loads nested guides on its own, so read them explicitly.
- Guides only match what you have checked out, so start from the latest `main` (see Branches, commits and pull requests).

| Guide | Covers |
| --- | --- |
| `app/AGENTS.md` | Foundation modules (config, db, models, errors, roles, clock, sessions, audit) and the data model: the system's tables and enums |
| `app/api/AGENTS.md` | The system's FastAPI app: identity headers, the status page, dashboard, health check, test-control routes, project routes, the 405 and 501 routes, and the 501 catch-all |
| `app/services/AGENTS.md` | Business logic: the steps and error order every operation follows, sessions, roles, dates and versions, and what each operation does, test control and the 405 and 501 operations included |
| `tester/AGENTS.md` | Our tester's FastAPI app, its session storage and cross-team test cases, and the rule that it never imports `app` |
| `fixtures/AGENTS.md` | The incident, ticketing and SIEM fixtures our tester sends |
| `tests/AGENTS.md` | The pytest suite: what each file covers, shared helpers and fixtures, and how to run it |
| `scripts/AGENTS.md` | `setup_db.py`: the roles, grants and checks it applies, and what to update when changing it; `backfill_deadlines.py`: which missing due dates it fills, and when to run it |
| `.github/AGENTS.md` | CI workflows, their scripts and `CODEOWNERS` |

**Keeping the guides current (mandatory)**

- Describe each behavior once, in the guide nearest the code that implements it. The CI check below then makes every change to that code update the guide that describes it. This file covers only files outside a guided folder and rules that span the repo.
- Every PR updates the relevant `AGENTS.md` for each file it changes: the nearest one in the file's folder or a parent folder. Files outside a guided folder, such as `README.md`, `server.py` or `pyproject.toml`, map to this root file.
- Make the update in the same PR as the change, so each guide describes the code as merged: new modules, behavior, rules and anything the next person needs to know.
- If a change truly alters nothing a guide says, don't add a filler edit. Put one line per guide in the PR description instead, naming the guide and the reason, e.g. `AGENTS.md: no change needed because the README edit only fixes a typo`. Reviewers judge whether the reason holds.
- Write guides as the current state, and fix or delete what the change made wrong. Never add history or "changed X" notes; `git log` holds history, and a guide that grows into a changelog buries the rules.
- Guides describe code, not intent: no plans, pending or unbuilt work, scope lists, phase labels, owners, or reasons quoted from the API specs. Requirement and acceptance-criterion IDs (`REQ-22`, `AC-09`) may appear as labels on the code that carries them.
- Don't restate one guide's content in another; point to the section instead, so the two can't drift apart.
- Every top-level folder except `.claude/` and `.agents/` (covered here), and every Python package under `app/`, needs its own `AGENTS.md`. Add it, and a row in the table above, in the PR that creates the folder.
- The `AGENTS.md updated` CI check (`.github/workflows/agents-md.yml`) enforces the update rule (accepting a skip line in its place) and the own-guide rule above, failing the PR otherwise; whitespace-only edits don't count, and a skip line can't stand in for a missing guide. Run it locally before pushing (see Branches, commits and pull requests).
- The check proves a guide changed, not that it's right; reviewers check the content against the code.
- A guide doesn't guarantee a rule is followed. Where a test or script can check a rule (like the AC-09 test or `setup_db.py`'s privilege check), add one.

## Stack, repo layout and config

The business logic is independent of the web framework; the FastAPI layer (routes, request handling, error handlers) lives in `app/api/`.

**Stack**

- Python 3.12, PostgreSQL, SQLAlchemy 2.0 with psycopg 3, pydantic-settings and pytest.
- FastAPI for the system's API layer and for the tester, both served by `server.py`.
- httpx2 for the tester's calls to target systems, and jsonschema with PyYAML to check their responses against `system-openapi.yaml`.
- Vercel runs the deployed service and Neon hosts its Postgres (see Deployment).
- Dependencies are declared in `pyproject.toml`.
- The `holidays` library supplies the U.S. federal holiday calendar used by disclosure deadline calculations.

**Repo layout**

- `system-openapi.yaml`: the Tracer Shared System API, copied verbatim from the version shared with Team 10 (its `info.version` names it). Don't edit it here; when the pod agrees on a new version, replace the whole file. The status page, the tester and the tests read it.
- `tester-openapi.yaml`: the Tracer Shared Tester API, copied and replaced the same way. The tester reads it.
- `app/`: the system's foundation modules (`app/AGENTS.md`), its FastAPI app `system_app` in `app/api/`, and its business logic in `app/services/`.
- `server.py`: the deployed ASGI app; it mounts the system at `/system` and the tester at `/tester`, and serves a landing page at `/` (see Deployment).
- `tester/`: our tester's FastAPI app, `tester_app`, its session storage, and the cross-team test cases it runs against a target system; it never imports `app`.
- `fixtures/`: the incident, ticketing and SIEM fixtures our tester sends.
- `scripts/setup_db.py`: creates the app and tester roles, the tables and the grants (see Database roles).
- `scripts/backfill_deadlines.py`: fills missing 8-K due dates on existing Domestic Material incidents, as the app role.
- `docker-compose.yml`: the local Postgres (see Local database).
- `tests/`: the pytest suite.
- `.github/`: CI workflows and their scripts, including the `AGENTS.md` check.

**Config** (environment variables, loaded from a git-ignored `.env`)

- `DATABASE_URL`: the app role's connection, used by the service and tests, e.g. `postgresql+psycopg://tracer_app:pass@127.0.0.1:5432/tracer`.
- `TESTER_DATABASE_URL`: the tester role's connection, used by the tester's session storage and tests and never read by `app/config.py`, e.g. `postgresql+psycopg://tracer_tester:pass@127.0.0.1:5432/tracer`.
- `ADMIN_DATABASE_URL`: the owner role's connection, used only by `scripts/setup_db.py` and never read by `app/config.py`, e.g. `postgresql+psycopg://tracer_owner:pass@127.0.0.1:5432/tracer`.
- `.env.example` holds placeholder values; never commit real credentials.

**Local database (Docker)**

- `docker-compose.yml` runs Postgres locally with `docker compose up -d`; everyone uses it, so tests run against the same Postgres version.
- It uses the `postgres:18` image, publishes port 5432 on loopback only (`127.0.0.1:5432:5432`; Docker otherwise listens on every host address), and keeps data in the named volume `pgdata`, so it survives restarts; `docker compose down -v` wipes it.
- It reads `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` from `.env`, with placeholders in `.env.example`.
- The container's `POSTGRES_USER` is the owner role: point `ADMIN_DATABASE_URL` at it, then run `python scripts/setup_db.py` to create `tracer_app`, `tracer_tester`, the tables and the grants.
- Point `DATABASE_URL` and `TESTER_DATABASE_URL` at the container as `tracer_app` and `tracer_tester`, never as `POSTGRES_USER`; it's a superuser and skips every permission check.

**Local setup steps**

Run these in order when someone asks to set up the project or the local database.

1. Check Docker Desktop is installed and running: `docker version` must show a Server section. If `docker` isn't found right after an install, open a new terminal; old ones keep the old PATH.
2. Install the project into a virtual environment: `python -m venv .venv`, activate it, then `pip install -e ".[dev]"`.
3. If `.env` doesn't exist, copy `.env.example` to `.env` and replace each `change-me-*` placeholder with a random value, e.g. `python -c "import secrets; print(secrets.token_urlsafe(24))"`. The owner password appears twice (`POSTGRES_PASSWORD` and `ADMIN_DATABASE_URL`) and must match; URL-safe values need no escaping. Never print, commit or overwrite an existing `.env`.
4. Start the database with `docker compose up -d` and wait until `docker compose ps` shows `healthy`.
5. Run `python scripts/setup_db.py`. It prints the app and tester roles' privileges; judge the result by the exit code. 0 is success, and the last line starts with `setup_db: done;`. 1 is failure: a message starting `setup_db:` says what went wrong and whether anything was changed, except for a missing variable, a malformed URL or a bare `postgresql://` scheme, which end in a Python traceback instead (see `scripts/AGENTS.md`).

- Re-run `scripts/setup_db.py` after adding a table or changing the grants; it is safe to re-run.
- A re-run creates missing tables and types and adds new enum values; it doesn't change existing columns or remove enum values. After changing a column, or removing or renaming an enum value, in `app/models.py` or `tester/models.py`, wipe the local database with `docker compose down -v` and redo steps 4 and 5. A deployed database needs a migration instead (see Deployment).
- `docker compose stop` keeps the data; `docker compose down -v` wipes it, after which steps 4 and 5 rebuild it.
- The image reads `POSTGRES_PASSWORD` only when it initializes an empty volume, so changing it later has no effect: wipe with `docker compose down -v` and redo steps 4 and 5.

## Database roles

Three roles keep the grants enforceable: a table's owner can always grant itself more, and a superuser skips permission checks, so neither the service nor the tester connects as either.

| Role | Connects via | Owns the tables | Used by |
| --- | --- | --- | --- |
| Owner (e.g. `tracer_owner`) | `ADMIN_DATABASE_URL` | Yes | `scripts/setup_db.py` only |
| App (`tracer_app`) | `DATABASE_URL` | No | The service, tests and `scripts/backfill_deadlines.py` |
| Tester (`tracer_tester`) | `TESTER_DATABASE_URL` | No | The tester's session storage and tests |

- `scripts/setup_db.py` creates both login roles, all seven tables and the grants, and fails unless each role ends up with exactly its expected privileges; `scripts/AGENTS.md` lists the grants and checks. Neither login role can DELETE or TRUNCATE anything, and the app role has only INSERT and SELECT on `audit_entries` (REQ-22, AC-23).
- Neither the service nor the tester creates tables at startup; their roles can't.
- The owner role needs permission to create tables and roles. Locally that's the container's superuser; on Neon it's `neondb_owner` (see Deployment).

## Deployment

Vercel runs the service and Neon's free plan hosts its Postgres: Vercel gives an HTTPS URL that stays the same for the semester, and Neon keeps the data across redeploys. Vercel builds the FastAPI app in `server.py`; Neon and `scripts/setup_db.py` don't depend on it.

**Neon (database)**

- The project was created on neon.com, not through Vercel's Neon integration. The integration sets its own `DATABASE_URL`, pooled and possibly logging in as the owner, and can give each preview deployment its own database branch.
- It runs Postgres 18, matching the local image, in AWS US East (N. Virginia), next to Vercel's default function region `iad1`.
- The project's `neondb_owner` role is the owner: point `ADMIN_DATABASE_URL` at it. It can create roles but isn't a superuser, so `setup_db.py` can't clear forbidden attributes here and fails instead (see `scripts/AGENTS.md`). It belongs to `neon_superuser`, so it can delete rows in every table; the grants restrict only `tracer_app` and `tracer_tester`.
- Let `setup_db.py` create `tracer_app` and `tracer_tester`; never create them in the Neon console. Roles made there join `neon_superuser`, which brings BYPASSRLS and write access, including DELETE, to every table, and `neondb_owner` can't alter them, so the run fails. Don't create helper roles there either.
- Use the direct connection string (the host without `-pooler`). Neon's Connect dialog shows the pooled one by default, so turn off "Connection pooling" there. Change its scheme from `postgresql://` to `postgresql+psycopg://` and keep its query parameters, including `sslmode=require` and `channel_binding=require` when present; with the bare scheme, SQLAlchemy looks for psycopg2, which isn't installed. Our traffic is far below the direct connection limit, and Neon recommends direct connections for schema changes such as `setup_db.py`'s.
- Neon rejects passwords with less than 60 bits of entropy; one from `secrets.token_urlsafe(24)` passes.
- The free plan pauses the database after 5 idle minutes and drops its connections. The first request after a pause waits a few hundred milliseconds for it to wake, and `app/db.py` replaces the dropped connections.
- A new table, grant or enum value needs only a `setup_db.py` re-run (below). Any other schema change needs a migration, not a wipe. Try either on a Neon branch first.

**Running `setup_db.py` against Neon**

- Run it from a dev machine before the service first starts and whenever the tables or grants change. Vercel has no separate setup step, so `ADMIN_DATABASE_URL` never goes on Vercel.
- Set `ADMIN_DATABASE_URL`, `DATABASE_URL` and `TESTER_DATABASE_URL` to the Neon URLs as environment variables for that one run, then clear them. Environment variables override `.env`, so `.env` keeps pointing at the local database.
- Set all three: any left unset still comes from `.env` and points at the local database.
- Judge the result by the exit code, as in local setup step 5.

**Vercel (service)**

- This GitHub repo is imported as a Vercel project. Production deploys from `main`. The system's base URL is `https://<project>.vercel.app/system` and the tester's is `https://<project>.vercel.app/tester`. Vercel names the production domain after the project, first come first served, so read the actual domain from Settings → Domains.
- `server.py` serves the domain's root, `GET /`: a landing page titled "Team 7 — Tracer" with buttons to `/system/health`, `/system/` and `/tester/`, in the same colors as the system and tester pages. It never touches the database, so it also works on previews. The outer app has no generated docs, so `/docs` and `/openapi.json` at the root are 404s; the mounted apps serve their own.
- `DATABASE_URL` (as `tracer_app`) and `TESTER_DATABASE_URL` (as `tracer_tester`) are set in the project's environment variables, for the Production environment only, never in the repo. PR preview deployments then have no database; sharing it would leave every preview request's rows in it, and neither login role can delete them.
- Both are Secret variables (write-only after saving). Redeploy after setting or changing any variable; changes only reach new deployments.
- Never set `ADMIN_DATABASE_URL` on Vercel: every variable there reaches the running function.
- On the Hobby plan, only the project's owner sees its variables and logs, and runtime logs are kept for 1 hour.
- Vercel installs from `pyproject.toml` and runs the Python version in `.python-version` (3.12, matching local). It loads `server:app` through `[tool.vercel]` `entrypoint`; without that it would auto-detect files such as `app.py` or `main.py` at the root or inside `app/`.
- Deployment Protection stays at Standard: the production domain is public for testers, while preview and deployment-specific URLs need a Vercel login.
- There's no `vercel.json`: the Hobby plan's default function limit of 300 s already covers the tester's longest request.
- After a production deploy, `GET /system/health` returns 200 with `"database": "ok"` once the function reaches Neon. A 503 means the database can't be reached, `DATABASE_URL` is missing or malformed, or the URL names a driver we don't install (a bare `postgresql://`); the function log names the cause. Preview deployments have no `DATABASE_URL`, so a team member logged in to Vercel sees 503 there.
- The server clock (UTC, NTP-synced) is Vercel's. Stored timestamps are timezone-aware UTC from `clock.utcnow()`, so the function's time zone setting doesn't matter.

## Branches, commits and pull requests

- Start each piece of work from the latest `main`: `git switch main`, `git pull`, then create the branch. On a branch that has been open a while, merge the latest `main` in before relying on the guides.
- Name branches per [Conventional Branch 1.1.0](https://conventionalbranch.org), e.g. `feature/group-a-contracts` or `chore/conventional-skills`; the `conventional-branch` skill holds the spec.
- Don't put usernames in branch names.
- Follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/); the `conventional-commits` skill holds the spec.
- On top of the spec, every commit message and PR title must include a scope: `type(scope): description`, e.g. `feat(db): add audit_entries table` or `docs(agents): add database roles`.
- `type: description` without a scope is valid under the spec but not in this repo.
- Use a short lowercase noun for the area changed, e.g. `agents`, `skills`, `config`, `db`, `models`, `roles`, `audit`, `incidents`, `evidence`, `workflow`, `api`.
- Write PR descriptions per the `pull-requests` skill.
- Every PR updates the relevant `AGENTS.md` files or gives a skip line for each (see Directory guides); the `AGENTS.md updated` check fails it otherwise. `.github/AGENTS.md` gives the skip line's exact format.
- Skills live in two folders with identical contents: `.claude/skills/` for Claude Code and `.agents/skills/` for Codex. Change both together; the `Skills in sync` CI check (`.github/workflows/skills.yml`) fails a PR when they differ.
- Before pushing, commit, fetch `origin/main`, and run both checks:
  - `python .github/scripts/check_agents_md.py --body "<PR description>"` (the body is only needed for skip lines)
  - `git diff --no-index --exit-code .claude/skills .agents/skills`
