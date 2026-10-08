# Tracer Phase 3 — Team 7 Implementation

Oct 4, 2026 · @t

## Stack, repo layout and config

The business logic is built first, independent of the web framework; the FastAPI layer (routes, request and response models, error handlers) is added once the API is confirmed.

**Stack**

- Python 3.12, PostgreSQL, SQLAlchemy 2.0 with psycopg 3, pydantic-settings and pytest.
- FastAPI for the API layer, once the API is confirmed.
- Dependencies are declared in `pyproject.toml`.

**Repo layout**

- `app/config.py`: settings read from environment variables.
- `app/db.py`: engine, `Base`, and one transaction per operation.
- `app/models.py`: the tables in Data model and storage.
- `app/errors.py`: a `TracerError` exception carrying the error code, message and field; the API layer maps it to HTTP responses later.
- `app/audit.py`: the `audit()` helper.
- `app/services/`: business logic in `incidents.py`, `evidence.py` and `workflow.py` (determination and transitions), callable without any web framework. Each operation takes the acting user's ID and role as arguments.
- `scripts/setup_db.py`: creates the app role, the tables and the grants (see Database roles).
- `tests/`: one test file per service module.

**Config** (environment variables, loaded from a git-ignored `.env`)

- `DATABASE_URL`: the app role's connection, used by the service and tests, e.g. `postgresql+psycopg://tracer_app:pass@localhost:5432/tracer`.
- `ADMIN_DATABASE_URL`: the owner role's connection, used only by `scripts/setup_db.py`, e.g. `postgresql+psycopg://tracer_owner:pass@localhost:5432/tracer`.
- `REGISTRANT_TYPE`: `Domestic` or `FPI`, defaulting to `Domestic`.
- Commit a `.env.example` with placeholder values; never commit real credentials.

**Local database (Docker)**

- Run Postgres locally with `docker compose up -d` from a committed `docker-compose.yml`; everyone in every group uses it, so tests run against the same Postgres version.
- Use the `postgres:18` image, map port 5432, and keep data in a named volume so it survives restarts; `docker compose down -v` wipes it.
- Read `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` from `.env`, with placeholders in `.env.example`.
- The container's `POSTGRES_USER` is the owner role: point `ADMIN_DATABASE_URL` at it, then run `python scripts/setup_db.py` to create `tracer_app`, the tables and the grants.
- Point `DATABASE_URL` at the container as `tracer_app`, never as `POSTGRES_USER`; it's a superuser and skips every permission check.

## Scope

Phase 3 builds Team 7's Tracer service covering REQ-01, 02, 03, 06, 07, 08, 19, 22 and 23. The Phase 3 implementation plan is the source of truth until the final API arrives; then paths, schemas and error codes get rewired against it.

- In scope: incident creation, evidence import, the five-state machine, materiality determinations, the audit trail, deployment, and 405/501 stubs.
- Out of scope for now: test control (`/test/setup`, `/test/report`, `X-Test-Run`), shared fixtures and the cross-team test suite. These get their own plan.
- Out of scope (Phase 4): due dates, flags, filings, LLM drafts, stale-version rejection (REQ-21), and re-determination of DeterminedNotMaterial.

## Build order and work split

The foundation comes first because every endpoint depends on it; after that, three people can work at the same time.

1. Foundation: config, database, tables, the error type, role checks and the `audit()` helper.
2. Endpoints: incidents, then evidence, then determination, then transitions. The code can be written in parallel, but testing each one end to end needs the one before it.
3. Deployment.

| Group | Owns |
| --- | --- |
| A. Foundation | Config, database, local Docker database, tables, database roles and `setup_db.py`, error type, role checks, `audit()` helper, deployment |
| B. Incidents and evidence | Incident create and get, the 405s, evidence import |
| C. Workflow | Determination, transitions, audit endpoint, 501 stubs |

- Group A shares the model fields and helper signatures on day one so B and C can start against them.
- Until import and determination are merged, group C tests by inserting incidents and evidence directly into the database.

## Cross-cutting rules

Every operation follows these rules. Roles, validation, no partial writes, dates, registrant type and version live in the services now; identity headers and error order are applied in the API layer once the API is confirmed.

**Identity headers**

- Require `X-User-Id` (non-empty) and `X-User-Role` (Responder, DisclosureApprover, Board) on every route.
- Missing or invalid header → 400 `VALIDATION_ERROR` with `field` set to the header name.

**Roles**

- Build one permission map: Responder = GETs, create incident, import evidence, tag evidence, create group; DisclosureApprover = everything; Board = GETs only.
- Anything else → 403 `FORBIDDEN_ROLE`, with nothing written.

**Error order** (return the first that applies)

1. Missing or invalid headers → 400
2. Wrong role → 403
3. Unknown incident (or evidence) → 404
4. Invalid body → 400
5. Wrong state → 409

- Implement as an ordered pipeline so no handler can return these out of order.

**Validation errors**

- Return the `Error` schema: `error`, `message`, and `field`.
- Use path-style field names, e.g. `items[3].detected_at`, `evidence_refs`, `determination_date`.
- Validate `format: date` as real `YYYY-MM-DD` dates and `format: date-time` as ISO-8601.

**No partial writes (REQ-19)**

- Validate the whole request before writing anything.
- Wrap every mutating request, including its audit entry, in one DB transaction.
- Roll back on any error so stored data matches the pre-request state.

**Dates**

- Write one helper `today()` = the current UTC date; use it for every date rule. When test control is added, it switches to the run's `test_now`.
- Use real server time (UTC, NTP-synced) for `imported_at`, `recorded_at` and audit `timestamp`.

**Registrant type**

- Read `registrant_type` (Domestic or FPI) from config. It moves onto the run when test control is added.

**Version**

- New incidents start at `version: 1`.
- Increment by 1 on each determination and successful transition; evidence import leaves it unchanged.
- Accept `version` in request bodies and validate it as an integer ≥ 1, but do not reject stale values yet.

## Data model and storage

Four tables hold everything in Phase 3. IDs are opaque, globally unique strings (e.g. prefixed UUIDs), and all timestamps are UTC.

**incidents**

| Field | Type | Rules |
| --- | --- | --- |
| incident_id | text, primary key | generated by the server |
| title | text, not null | non-empty |
| discovery_date | date, not null | not after today |
| state | State enum, not null | starts as UnderAssessment |
| required_form | FormType enum, nullable | null until a Material determination |
| due_date | date, nullable | always null in Phase 3 |
| group_id | text, nullable | always null in Phase 3 |
| version | integer, not null | starts at 1; +1 on each determination and transition |

**determinations**

| Field | Type | Rules |
| --- | --- | --- |
| incident_id | text, primary key, references incidents | one per incident in Phase 3 |
| decision | `Material` or `NotMaterial`, not null | |
| determination_date | date, not null | on or after discovery_date, not after today |
| rationale | text, not null | non-empty |
| evidence_refs | JSON array of evidence IDs, not null | at least one, unique, all from this incident |
| voluntary_disclosure | boolean, not null | defaults to false |
| recorded_by | text, not null | the user who recorded it |
| recorded_at | timestamp, not null | real server time |

**evidence**

| Field | Type | Rules |
| --- | --- | --- |
| evidence_id | text, primary key | generated by the server |
| incident_id | text, not null, references incidents | |
| source | `ticketing` or `siem`, not null | |
| source_ref | text, not null | from ticket_id or alert_id |
| summary | text, not null | from summary or rule_name |
| observed_at | timestamp, not null | from created_at or detected_at |
| imported_by | text, not null | the importing user |
| imported_at | timestamp, not null | real server time |
| impact_categories | JSON array of ImpactCategory, not null | defaults to empty |
| raw_item | JSON, not null | the original source item, kept for traceability |

**audit_entries**

| Field | Type | Rules |
| --- | --- | --- |
| seq | bigint identity, primary key | sets "oldest first" order |
| entry_id | text, unique, not null | generated by the server |
| user_id | text, not null | the acting user |
| timestamp | timestamp, not null | real server time |
| action | AuditAction enum, not null | |
| target_id | text, not null | the incident ID |
| before | JSON, nullable | null on create and import |
| after | JSON, not null | changed fields, including version |

**Enums**

- State: UnderAssessment, DeterminedMaterial, DeterminedNotMaterial, DisclosureDelayed, DisclosureRecorded, AmendmentPending, Closed.
- FormType: `8-K Item 1.05`, `8-K Item 8.01`, `6-K`, `8-K/A`.
- ImpactCategory: operational, financial, legal_regulatory, reputational, data_compromised.
- AuditAction: create_incident, import_evidence, record_determination, transition (later phases add tag_evidence, record_filing, accept_draft and create_group).

**Storage rules**

- Grant the app's DB user only INSERT and SELECT on audit_entries, and no DELETE on incidents or evidence (REQ-22). This is what the AC-23 permission demo shows.
- Use a persistent, managed database so data survives redeploys.

**Database roles**

Two roles keep the REQ-22 grants enforceable: a table's owner can always grant itself more, and a superuser skips permission checks, so the service must never connect as either.

| Role | Connects via | Owns the tables | Used by |
| --- | --- | --- | --- |
| Owner (e.g. `tracer_owner`) | `ADMIN_DATABASE_URL` | Yes | `scripts/setup_db.py` only |
| App (`tracer_app`) | `DATABASE_URL` | No | The service and tests |

- `scripts/setup_db.py` runs as the owner and is safe to re-run. It creates the app role from the user and password in `DATABASE_URL` if missing, runs `Base.metadata.create_all`, then applies the grants.
- Grant the app role exactly:
  - USAGE on the schema.
  - `incidents`: SELECT, INSERT, UPDATE.
  - `evidence`: SELECT, INSERT (Phase 4 adds UPDATE for tagging).
  - `determinations`: SELECT, INSERT.
  - `audit_entries`: SELECT, INSERT.
- Nothing else: no DELETE or TRUNCATE anywhere, and no UPDATE on evidence, determinations or audit_entries.
- The service never creates tables at startup; the app role can't.
- The owner role needs permission to create tables and roles. Locally that's the container's superuser; on the host, use the managed database's admin user.
- Use the setup script in place of migrations for Phase 3; move to Alembic once the schema starts changing.

## Incidents (REQ-01, REQ-22)

Incidents are created in UnderAssessment at version 1 and are never replaced or deleted.

**`POST /incidents`** (Responder, DisclosureApprover)

- Require `title` (non-empty string) → else 400 `field: "title"`.
- Require `discovery_date` as a valid date not after `today()` → else 400 `field: "discovery_date"`; equal to today is valid.
- Insert with `state: UnderAssessment`, `determination: null`, `required_form: null`, `due_date: null`, `flags: []`, `filings: []`, `evidence: []`, `group_id: null`, `version: 1`.
- Write a `create_incident` audit entry: `before: null`, `after: {state, title, discovery_date, version}`.
- Return 201 with the full Incident.
- Board → 403.

**`GET /incidents/{incident_id}`** (all roles)

- Return the full Incident; unknown ID → 404.
- Always return `flags: []` and `due_date: null` in Phase 3.

**`PUT` and `DELETE /incidents/{incident_id}`**

- Return 405 `METHOD_NOT_ALLOWED` and change nothing.
- Register these routes explicitly so the framework doesn't fall back to its own 404 or 405 body.

## Evidence import (REQ-02, REQ-03)

Each request is one `ImportRequest` body of simulated ticketing or SIEM items; one bad item rejects the whole batch, and import never changes the incident's version.

**`POST /incidents/{incident_id}/evidence/import`** (Responder, DisclosureApprover)

- Validate `source` ∈ {ticketing, siem} and `items` as a non-empty array.
- For `ticketing`, require each item's `ticket_id`, `summary` and `created_at` (date-time).
- For `siem`, require `alert_id`, `rule_name`, `severity` ∈ {low, medium, high, critical} and `detected_at` (date-time).
- Reject an item shaped for the other source as invalid.
- On the first bad item, return 400 with `field` like `items[3].detected_at` and write nothing.
- Normalize each item to an `EvidenceItem`:
  - `source_ref` ← `ticket_id` / `alert_id`
  - `summary` ← `summary` / `rule_name`
  - `observed_at` ← `created_at` / `detected_at`
  - `imported_by` ← `X-User-Id`
  - `imported_at` ← real server time (AC-02 allows ±5 s)
  - `impact_categories` ← `[]`
- Insert all items in one transaction; allow import in any state, including Closed.
- Do not change `version`.
- Write one `import_evidence` audit entry per request: `target_id` = incident ID, `before: null`, `after: {source, evidence_ids}`.
- Return 201 `{"evidence": [...]}` in input order.
- Board → 403; unknown incident → 404.

**Other evidence routes**

- `DELETE /incidents/{id}/evidence/{evidence_id}` → 405, nothing changed.
- `PATCH /incidents/{id}/evidence/{evidence_id}` → 501 unless REQ-05 is built (see Stubs and not-yet-built routes).

## Materiality determination (REQ-07, REQ-08)

Only a DisclosureApprover can record a determination, only from UnderAssessment, and this is the only code path that writes a materiality value.

**`POST /incidents/{incident_id}/determination`**

- Responder or Board → 403, nothing written (AC-08).
- Require `decision` ∈ {Material, NotMaterial}, `determination_date`, non-empty `rationale`, `evidence_refs` and `version`; a missing field → 400 naming it.
- Require `determination_date` ≥ `discovery_date` and ≤ `today()`; both boundaries are valid (AC-20).
- Require `evidence_refs` to have at least one unique ID, each belonging to this incident; an unknown ref or one from another incident → 400 `field: "evidence_refs"`.
- If `voluntary_disclosure` is present and REQ-18 isn't built → 501.
- State not UnderAssessment → 409 `INVALID_TRANSITION` (checked after body validation, per error order).
- Set `state` to DeterminedMaterial or DeterminedNotMaterial.
- Set `required_form` from the configured registrant type: Material + Domestic → `8-K Item 1.05`; Material + FPI → `6-K`; NotMaterial → `null`.
- Keep `due_date: null` and `flags: []`.
- Store the determination with `voluntary_disclosure: false` by default, `recorded_by` from `X-User-Id`, and `recorded_at` as server time.
- Increment `version`.
- Write a `record_determination` audit entry: `before: {state, determination: null, version}`, `after: {state, determination: {decision, determination_date}, required_form, version}`.
- Return 200 with the full Incident.

**Guardrails for REQ-08 (AC-09)**

- Keep materiality writes inside this one handler; import and transitions must never touch `determination`.
- Add a unit test asserting no other handler writes the determination table or field.

## State machine and transitions (REQ-06)

Only five moves succeed in Phase 3; every other pair, including staying in the same state, returns 409 `INVALID_TRANSITION` with nothing changed.

| From | To | Endpoint |
| --- | --- | --- |
| UnderAssessment | DeterminedMaterial or DeterminedNotMaterial | `/determination` |
| DeterminedMaterial | DisclosureDelayed | `/transitions` |
| DisclosureDelayed | DeterminedMaterial | `/transitions` |
| DeterminedNotMaterial | Closed | `/transitions` |
| Closed | nothing | — |

**`POST /incidents/{incident_id}/transitions`** (DisclosureApprover only)

- Responder or Board → 403.
- Validate `to` against the full `State` enum (an unknown value → 400) and `version` as an integer ≥ 1.
- Encode the allowed set as data: `{(DM, DisclosureDelayed), (DisclosureDelayed, DM), (DNM, Closed)}`; check `(current, to)` against it.
- Return 409 for every pair not in the set, including UA → DM/DNM (those go through `/determination`), DNM → DM (re-determination is Phase 4) and any `to: DisclosureRecorded` (filings, Phase 4).
- On success, set `state`, increment `version`, keep `determination` and `required_form` untouched, and return 200 with the full Incident.
- Write a `transition` audit entry: `before: {state, version}`, `after: {state, version}`.
- For `to: AmendmentPending`, validate that `info_available_date` and `elements` are present and well formed first (400 if not), then return 409 for the disallowed pair.

## Audit trail (REQ-22)

Every successful create, import request, determination and transition writes exactly one append-only entry; failed requests write none.

- Write one helper `audit(tx, action, target_id, before, after)` and call it inside the same transaction as the change.
- Fill `entry_id`, `user_id` from `X-User-Id`, `timestamp` as server time, `action`, `target_id`, `before` and `after`.
- Include `version` in `before` and `after` for incident changes.
- Use the shapes defined above: `create_incident`, `import_evidence` (one per request), `record_determination`, `transition`.
- `GET /incidents/{incident_id}/audit` (all roles): return `{"entries": [...]}` oldest first by sequence; unknown incident → 404.
- Include entries whose `target_id` is the incident, including import entries.
- `PUT`, `PATCH`, `DELETE /incidents/{incident_id}/audit` → 405, nothing changed.
- Enforce append-only in the DB as well (INSERT/SELECT grant only) and be ready to demo it for AC-23.
- Don't audit GETs or rejected requests.

## Stubs and not-yet-built routes

Anything not built returns 501 `NOT_IMPLEMENTED` using the `Error` schema.

- `PATCH /incidents/{id}/evidence/{evidence_id}` (REQ-05) → 501.
- `POST /groups` (REQ-04) → 501.
- `voluntary_disclosure` on a determination (REQ-18) → 501.
- Run the usual header and role checks before returning 501, so error order stays consistent.

## Deployment

The service needs a stable HTTPS base URL and a database that keeps its data across redeploys.

- Deploy behind HTTPS at a URL that won't change for the rest of the semester.
- Store `DATABASE_URL`, `ADMIN_DATABASE_URL` and any other secrets in the host's settings, never in the repo.
- Use a persistent database that survives redeploys.
- Run the server clock in UTC with NTP sync (AC-02's ±5 s).
- Run `scripts/setup_db.py` as the owner as part of deploy, which applies the grants.

## Decisions settled by the Phase 3 plan

- `voluntary_disclosure`: the plan returns 501 whenever it is sent, so any value, including `false`, returns 501 while REQ-18 isn't built.
- 405 routes: the plan lists their roles as "All", so header and role checks run first, then 405.
- `to: AmendmentPending`: the plan's error order puts invalid body (400) before wrong state (409). Missing or malformed `info_available_date` or `elements` → 400; with valid fields, the disallowed pair → 409.

## Tests to write

Write pytest tests against the service modules using a real local Postgres database. Every rejection test also checks that stored data is unchanged; HTTP-level tests (status codes, headers, error order) come with the API layer.

**Incidents**

- A new incident is UnderAssessment with version 1 and all other fields at their defaults.
- 100 incidents get 100 distinct IDs.
- A missing or empty title is rejected with `VALIDATION_ERROR` on `title`.
- A discovery_date after today is rejected; one equal to today is accepted.
- Board cannot create an incident (`FORBIDDEN_ROLE`).
- Each create writes one `create_incident` audit entry.

**Evidence**

- Ticketing and SIEM items normalize to the right `source_ref`, `summary`, `observed_at`, `imported_by` and `source`.
- `imported_at` is within 5 seconds of server time.
- 20 ticketing and 20 SIEM items produce 40 evidence items.
- One bad item (missing field, bad severity, malformed timestamp, wrong shape for the source) rejects the whole batch, names the field, and stores nothing.
- Import works in every state, including Closed, and never changes version.
- Each import writes exactly one `import_evidence` audit entry listing the source and new evidence IDs.

**Determination**

- A complete Material determination sets DeterminedMaterial and the right `required_form` for Domestic and for FPI; NotMaterial sets DeterminedNotMaterial with `required_form` null.
- Only DisclosureApprover can record one; Responder and Board get `FORBIDDEN_ROLE`.
- Each missing field, zero evidence refs, an unknown ref and a ref from another incident are rejected.
- A date before discovery or after today is rejected; dates equal to discovery and to today are accepted.
- A second determination, or one from any state other than UnderAssessment, gets `INVALID_TRANSITION`.
- Sending `voluntary_disclosure` gets `NOT_IMPLEMENTED`.
- Success increments version by 1 and writes one `record_determination` audit entry.

**Transitions**

- DM → DisclosureDelayed, DisclosureDelayed → DM and DNM → Closed succeed, increment version and write one `transition` audit entry.
- Every other target from UnderAssessment, DM, DNM, DisclosureDelayed and Closed gets `INVALID_TRANSITION` with state and version unchanged, including staying in the same state and DNM → DM.
- After imports and transition attempts on an UnderAssessment incident, `determination` is still empty and state is still UnderAssessment.

**Audit**

- A 10-action sequence produces 10 matching entries, oldest first.
- Under the app's DB user, UPDATE and DELETE on audit_entries fail, and DELETE on incidents and evidence fails.

## Done checklist

Phase 3 is done for Team 7 when the deployed service does all of the following.

- Creates incidents with distinct IDs in UnderAssessment.
- Imports ticketing and SIEM items as normalized evidence, rejecting any batch with a bad item.
- Records a determination only as DisclosureApprover and sets the correct `required_form`.
- Rejects disallowed transitions with 409 and leaves state unchanged.
- Writes an audit entry for every action and rejects edits and deletes with 405.
- Leaves stored data unchanged on every error.
- Is ready to demo the audit table's DB permissions (AC-23).

## Branches, commits and pull requests

- Name branches per [Conventional Branch 1.1.0](https://conventionalbranch.org), e.g. `feature/group-a-contracts` or `chore/conventional-skills`; the `conventional-branch` skill holds the spec.
- Don't put usernames in branch names.
- Follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/); the `conventional-commits` skill holds the spec.
- On top of the spec, every commit message and PR title must include a scope: `type(scope): description`, e.g. `feat(db): add audit_entries table` or `docs(agents): add database roles`.
- `type: description` without a scope is valid under the spec but not in this repo.
- Use a short lowercase noun for the area changed, e.g. `agents`, `skills`, `config`, `db`, `models`, `roles`, `audit`, `incidents`, `evidence`, `workflow`, `api`.
- Write PR descriptions per the `pull-requests` skill.
- Skills live in two folders with identical contents: `.claude/skills/` for Claude Code and `.agents/skills/` for Codex. Change both together.
