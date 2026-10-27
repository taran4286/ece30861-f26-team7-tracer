# app/services/: business logic

One module per area, callable without any web framework. Each project operation takes the acting user's ID and role and the request's session ID as keyword arguments; test-control operations take only a body or a session ID. The API layer checks the identity headers before calling any of them (see `api/AGENTS.md`).

| Module | Functions and constants | Tests |
| --- | --- | --- |
| `incidents.py` | `create_incident(body, …)`, `get_incident(incident_id, …)`, and the shared `load_incident` and `*_to_dict` builders | `tests/test_incidents.py` |
| `evidence.py` | `import_evidence(incident_id, body, …)`, `load_evidence`, `SHAPES` (where each source's fields come from) and `_FOREIGN_KEYS` | `tests/test_evidence.py` |
| `workflow.py` | `record_determination(incident_id, body, …)`, `transition_incident(incident_id, body, …)`, `list_audit_entries(incident_id, …)`, `audit_entry_to_dict`, `ALLOWED_TRANSITIONS` and `DRAFT_ELEMENTS` | `tests/test_workflow.py` |
| `control.py` | `setup_session(body)`, `coverage_report(session_id)`, `snapshot(run_id)`, `run_to_dict`, `DEFAULTS`, `INTEGER_MAX` and `CODE_COVERAGE_BASIS` | `tests/test_control.py` |
| `placeholders.py` | `refuse_change(incident_id, evidence_id=None, …)` (405) and `not_built(operation, incident_id=None, evidence_id=None, …)` (501) | `tests/test_placeholders.py` |
| `validation.py` | Shared body checks: `require_object`, `require_string`, `parse_integer`, `parse_date`, `parse_date_time`, `invalid` | Covered through the service tests |

Add a row for each new module.

## Shape of every operation

Each project operation takes one keyword-only argument per identity header, `*, user_id, role, session_id`, and runs these steps in this order inside one `transaction()`. The first that fails decides the response, which gives the error order: headers (API layer, 400), session (404), role (403), path IDs (404), body (400), state (409).

1. `load_session(tx, session_id)`: 404 `NOT_FOUND` for an unknown session.
2. `require_role(role, Operation.X)`: 403 `FORBIDDEN_ROLE`.
3. Load the incident with `load_incident(tx, incident_id, session_id=session_id)`, and any evidence named in the path: 404 if unknown or in another session.
4. Validate the whole body: 400 `VALIDATION_ERROR` on the first bad field, before anything is written. Date rules compare against `clock.today(session)`.
5. Check the state: 409 `INVALID_TRANSITION`.
6. Write the change and call `audit(..., session_id=session_id)`. Any error raised inside the transaction rolls back everything, including the audit entry, so a rejected request leaves stored data as it was (REQ-19).

```python
def record_determination(
    incident_id: str, body: Any, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.RECORD_DETERMINATION)
        incident = load_incident(tx, incident_id, session_id=session_id, for_update=True)
        body = require_object(body)
        ...  # validate every field; raise invalid(field, message)
        if incident.state != State.UNDER_ASSESSMENT:
            raise TracerError(ErrorCode.INVALID_TRANSITION, ...)
        ...  # write
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, after,
              user_id=user_id, session_id=session_id)
        return incident_to_dict(tx, incident)
```

**Sessions, roles, dates and versions**

- Every lookup is scoped to the request's session: an incident belongs to the session that created it and is unknown (404) in any other. Evidence and determinations belong to their incident, and each audit entry records its request's session.
- `roles.PERMISSIONS`: Responder may read, create incidents, import evidence, tag evidence and create groups; DisclosureApprover may do everything; Board may only read.
- Today is `clock.today(session)`, the UTC date of the session's `test_now`. The registrant type is `session.registrant_type`. Neither comes from config or the real date.
- New incidents start at `version: 1`. A determination and a successful transition each add 1; import leaves it unchanged. Bodies that carry `version` must send an integer ≥ 1, but a stale value is accepted.

**Validation**

- Errors name a path-style `field`: `title`, `items[3].detected_at`, `setup.test_now`, `elements[1]`. Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- `require_object` rejects a body that isn't a JSON object (a body that isn't JSON arrives as `None`).
- `require_string` rejects missing, non-string and blank (whitespace-only) values, and NUL characters because PostgreSQL text cannot store them.
- `parse_date` accepts only a real `YYYY-MM-DD`; `parse_date_time` accepts only RFC 3339 with seconds and a `Z` or `±HH:MM` offset (lowercase `t` and `z` included), returned in UTC.
- `parse_integer` accepts a whole-number float such as `1.0` and rejects booleans. Pass `maximum=control.INTEGER_MAX` for a value stored in an `Integer` column, so a value too large to store is a 400, not a failed insert.

## Shared helpers

- `incidents.load_incident(tx, incident_id, *, session_id, for_update=False)` loads the path incident or raises `NOT_FOUND`, treating another session's incident as unknown. `for_update=True` locks the row; the determination and transition operations use it, so two concurrent requests can't both pass the state check or bump `version` from the same value.
- `evidence.load_evidence(tx, incident_id, evidence_id)` loads evidence named in the path, after its incident, or raises `NOT_FOUND`; evidence of another incident counts as unknown.
- `incidents.incident_to_dict(tx, incident)` builds the full Incident (JSON-ready: dates and timestamps as ISO-8601 strings, enums as values), and every operation that returns an Incident uses it. `incidents_to_dicts(tx, incidents)` builds many at once with one query each for their determinations and evidence. `evidence_to_dict` and `determination_to_dict` build the parts; `raw_item` is never returned.
- An Incident always has `due_date: null`, `flags: []` and `filings: []`. Its `evidence` is ordered by `imported_at`, then `evidence_id`; items from one import share an `imported_at` and their IDs are random, so they don't come back in the order they were sent.
- `workflow.audit_entry_to_dict(entry)` builds an audit entry (without `seq` or `session_id`) for the audit list and the snapshot.

## Test control (`control.py`; TEST-01, TEST-02, TEST-06)

These take no identity headers or role and write no audit entries. They raise `TracerError` like the rest, and the API layer turns it into `{message, source: "system"}` (see `api/AGENTS.md`).

- `setup_session(body)` (`POST /test/setup`) checks the whole body before opening a transaction, so its 400s need no database:
  - `session_id` must be a non-blank string, and `setup` an object.
  - `setup` may hold only `test_now` (date-time), `registrant_type` (Domestic or FPI), `at_risk_threshold_days` (integer from 1 to `INTEGER_MAX`, 2,147,483,647) and `llm_base_url` (an absolute URI: a scheme, a colon, then no whitespace); any other key or a bad value is a 400 on `setup.<key>`.
  - A missing setting takes `DEFAULTS`: `test_now` 2026-10-05T15:00:00Z, Domestic, 7, and `https://llm.invalid/v1`.
  - It stores the session and returns `{session_id}`. An ID already in use is `CONFLICT` (409), including one stored by a concurrent setup after the existence check, caught on flush.
- `coverage_report(session_id)` (`GET /test/report?session_id=`) rejects a missing or blank `session_id` (400) before touching the database, then 404s an unknown session. It returns `{session_id, coverage: {code: {covered: null, total: null, basis}}}` with `basis` = `CODE_COVERAGE_BASIS`: code coverage isn't measured.
- `snapshot(run_id)` (`GET /test/report/{run_id}`) returns `{run, incidents, groups: [], audit}`: the session's TestRun from `run_to_dict` (`run_id` is the session ID, `implemented_optional: []`), its incidents as full Incidents ordered by `incident_id` (random, not creation order), and its audit entries ordered by `seq`. Unknown session → 404.

## Incidents (`incidents.py`; REQ-01, REQ-22)

- `create_incident` (Responder, DisclosureApprover) checks `title` (`require_string`), then `discovery_date` (a date not after today; equal to today passes). It inserts the incident in the request's session with `state: UnderAssessment`, `required_form`, `due_date` and `group_id` null and `version: 1`, writes a `create_incident` audit entry (`before: null`, `after: {state, title, discovery_date, version}`) and returns the full Incident.
- `get_incident` (all roles) returns the full Incident.

## Evidence import (`evidence.py`; REQ-02, REQ-03)

`import_evidence` (Responder, DisclosureApprover) takes one batch of ticketing or SIEM items; one bad item rejects the whole batch. It works in every state, Closed included, and never changes `version`.

- It checks `source` (ticketing or siem), then `items` (a non-empty array), then each item in order: an object (`items[i]`), then its fields in a fixed order (ticketing: `ticket_id`, `summary`, `created_at`; SIEM: `alert_id`, `rule_name`, `severity` ∈ low/medium/high/critical, `detected_at`).
- It then rejects the other source's keys listed in `_FOREIGN_KEYS` (`items[i].<key>`): ticketing rejects `alert_id`, `rule_name`, `severity` and `detected_at`; SIEM rejects `ticket_id` and `created_at`. Other extra keys, including `summary` on a SIEM item, are kept in `raw_item`.
- Each item becomes an evidence row: `source_ref` from `ticket_id`/`alert_id`, `summary` from `summary`/`rule_name`, `observed_at` from `created_at`/`detected_at`, `imported_by` from the user, `impact_categories: []`, `raw_item` as sent, and one `imported_at` (real server time) shared by the batch.
- It writes one `import_evidence` audit entry per request (`target_id` = incident ID, `before: null`, `after: {source, evidence_ids}`) and returns `{"evidence": [...]}` in input order.

## Materiality determination (`workflow.py`; REQ-07, REQ-08)

`record_determination` (DisclosureApprover only) is the only code that writes a materiality value.

- It checks, in order: `decision` (Material or NotMaterial); `determination_date` (a date, then on or after `discovery_date` and not after today, both ends included); `rationale` (`require_string`); `evidence_refs` (a non-empty array of unique, non-blank strings without NUL, then every ID must belong to this incident, both on `evidence_refs`); `version`; then `voluntary_disclosure` if sent: a non-boolean is a 400, and `true` or `false` is a 501 `NOT_IMPLEMENTED`.
- After those, a state other than UnderAssessment is 409 `INVALID_TRANSITION`, so a second determination is a 409.
- Material sets `DeterminedMaterial` and `required_form` from the session: `8-K Item 1.05` for Domestic, `6-K` for FPI. NotMaterial sets `DeterminedNotMaterial` and `required_form: null`.
- It stores the determination with `voluntary_disclosure: false`, `recorded_by` = the user and `recorded_at` = server time, adds 1 to `version`, writes a `record_determination` audit entry (`before: {state, determination: null, version}`, `after: {state, determination: {decision, determination_date}, required_form, version}`) and returns the full Incident.

## State transitions (`workflow.py`; REQ-06)

`transition_incident` (DisclosureApprover only) applies only the pairs in `ALLOWED_TRANSITIONS`:

| From | To |
| --- | --- |
| DeterminedMaterial | DisclosureDelayed |
| DisclosureDelayed | DeterminedMaterial |
| DeterminedNotMaterial | Closed |

- It checks `to` (a `State` name: a non-string or unknown name is a 400 on `to`), then `version`, then `info_available_date` (a date not after today) and `elements` (a non-empty array, else a 400 on `elements`; each entry one of `DRAFT_ELEMENTS` (nature, scope, timing, impact) and not repeated, else a 400 on `elements[i]`).
- `info_available_date` and `elements` are required when `to` is AmendmentPending and checked whenever they're sent for any other target.
- After the body, any pair not in the table is 409 `INVALID_TRANSITION` with nothing changed: staying in the same state, UnderAssessment → either Determined state (that goes through the determination), DeterminedNotMaterial → DeterminedMaterial, every move out of Closed, and every move to DisclosureRecorded or AmendmentPending (with valid fields).
- On success it sets `state`, adds 1 to `version`, leaves `determination` and `required_form` as they are, writes a `transition` audit entry (`before: {state, version}`, `after: {state, version}`) and returns the full Incident.

## Audit trail (`workflow.py`; REQ-22)

- Every successful create, import request, determination and transition writes exactly one audit entry through `app/audit.py`, in the same transaction; rejected requests and GETs write none.
- `list_audit_entries` (all roles) returns `{"entries": [...]}`: every entry whose `target_id` is the incident, import entries included, oldest first by `seq`.
- Entries are never updated or deleted: no code path does, the audit routes' PUT, PATCH and DELETE return 405 (below), and the app role has only INSERT and SELECT on `audit_entries` (see `scripts/AGENTS.md`).

## 405 and 501 operations (`placeholders.py`)

Each runs steps 1 to 3 (session, role, path IDs) in one transaction, then raises; it writes nothing and never reads the body, so a 405 or 501 never hides another error. `refuse_change` always checks `Operation.REPLACE_OR_DELETE` and raises 405 `METHOD_NOT_ALLOWED`; `not_built` checks the `Operation` it's given and raises 501 `NOT_IMPLEMENTED`.

| Operation | Function and `Operation` | Roles that pass | Path IDs looked up | Result |
| --- | --- | --- | --- | --- |
| `PUT`, `DELETE /incidents/{id}` | `refuse_change`, `REPLACE_OR_DELETE` | DisclosureApprover | incident | 405 |
| `DELETE /incidents/{id}/evidence/{evidence_id}` | `refuse_change`, `REPLACE_OR_DELETE` | DisclosureApprover | incident, evidence | 405 |
| `PUT`, `PATCH`, `DELETE /incidents/{id}/audit` | `refuse_change`, `REPLACE_OR_DELETE` | DisclosureApprover | incident | 405 |
| `PATCH /incidents/{id}/evidence/{evidence_id}` (REQ-05) | `not_built`, `TAG_EVIDENCE` | Responder, DisclosureApprover | incident, evidence | 501 |
| `POST /groups` (REQ-04) | `not_built`, `CREATE_GROUP` | Responder, DisclosureApprover | none | 501 |
| `POST /incidents/{id}/filings` | `not_built`, `RECORD_FILING` | DisclosureApprover | incident | 501 |
| `POST /incidents/{id}/drafts` | `not_built`, `REQUEST_DRAFT` | DisclosureApprover | incident | 501 |
| `POST /drafts/{draft_id}/accept` | `not_built`, `ACCEPT_DRAFT` | DisclosureApprover | none | 501 |

`voluntary_disclosure` on a determination is the other 501 (see Materiality determination).

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 checks, so the API layer must not reject a body before the service runs.
- Materiality is written only in `workflow.record_determination` (AC-09).
- Keep the allowed pairs in `ALLOWED_TRANSITIONS`; `transition_incident` checks only that set.
- An operation that ends in 405 or 501 goes in `placeholders.py`.
- Don't name a service function with a leading `test`: pytest would collect it from any test file that imports it. The snapshot's TestRun builder is `control.run_to_dict` for that reason.
