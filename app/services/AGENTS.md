# app/services/: business logic

One module per area, callable without any web framework. Each project operation takes the acting user's ID and role and the request's session ID as keyword arguments; test-control operations take only a session ID, a body, or both. The API layer checks the identity headers before calling any of them (see `api/AGENTS.md`).

| Module | Functions and constants | Tests |
| --- | --- | --- |
| `incidents.py` | `create_incident(body, …)`, `get_incident(incident_id, …)`, and the shared `load_incident` and `*_to_dict` builders | `tests/test_incidents.py` |
| `evidence.py` | `import_evidence(incident_id, body, …)`, `load_evidence`, `SHAPES` (where each source's fields come from) and `_FOREIGN_KEYS` | `tests/test_evidence.py` |
| `workflow.py` | `record_determination(incident_id, body, …)`, `transition_incident(incident_id, body, …)`, `list_audit_entries(incident_id, …)`, `audit_entry_to_dict`, `ALLOWED_TRANSITIONS` and `DRAFT_ELEMENTS` | `tests/test_workflow.py` |
| `control.py` | `setup_session(body)`, `coverage_report(session_id)`, `update_settings(session_id, body)`, `snapshot(run_id)`, `run_to_dict`, `DEFAULTS`, `MUTABLE_SETTINGS`, `INTEGER_MAX` and `CODE_COVERAGE_BASIS` | `tests/test_control.py` |
| `placeholders.py` | `refuse_change(incident_id, evidence_id=None, …)` (405) and `not_built(operation, incident_id=None, evidence_id=None, …)` (501) | `tests/test_placeholders.py` |
| `validation.py` | Shared body checks: `require_object`, `require_string`, `parse_integer`, `parse_date`, `parse_date_time`, `invalid` | Covered through the service tests |
| `filings.py` | `validate_filing_request` (pure validation), `record_filing` (storage, state and audit) | `tests/test_filings.py`, `tests/test_filing_storage.py` |
| `flags.py` | `incident_flags(incident, filings, session)` | `tests/test_flags.py` |
| `deadlines.py` | `federal_holidays`, `is_business_day`, `add_business_days`, `disclosure_due_date`, `MIN_CALENDAR_YEAR` and `MAX_CALENDAR_YEAR` | `tests/test_deadlines.py` |

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
- New incidents start at `version: 1`. A determination, successful transition and recorded filing each add 1; import leaves it unchanged. Bodies that carry `version` must send an integer ≥ 1, but a stale value is accepted.

**Validation**

- Errors name a path-style `field`: `title`, `items[3].detected_at`, `setup.test_now`, `elements[1]`. Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- `require_object` rejects a body that isn't a JSON object (a body that isn't JSON arrives as `None`).
- `require_string` rejects missing, non-string and blank (whitespace-only) values, and NUL characters because PostgreSQL text cannot store them.
- `parse_date` accepts only a real `YYYY-MM-DD`; `parse_date_time` accepts only RFC 3339 with seconds and a `Z` or `±HH:MM` offset (lowercase `t` and `z` included), returned in UTC.
- `parse_integer` accepts a whole-number float such as `1.0` and rejects booleans. Pass `maximum=control.INTEGER_MAX` for a value stored in an `Integer` column, so a value too large to store is a 400, not a failed insert.

## Shared helpers

- `incidents.load_incident(tx, incident_id, *, session_id, for_update=False)` loads the path incident or raises `NOT_FOUND`, treating another session's incident as unknown. `for_update=True` locks the row; determination, transition and filing operations use it, so two concurrent requests can't both pass the state check or bump `version` from the same value.
- `evidence.load_evidence(tx, incident_id, evidence_id)` loads evidence named in the path, after its incident, or raises `NOT_FOUND`; evidence of another incident counts as unknown.
- `incidents.incident_to_dict(tx, incident)` builds the full Incident (JSON-ready: dates and timestamps as ISO-8601 strings, enums as values), and every operation that returns an Incident uses it. `incidents_to_dicts(tx, incidents)` builds many at once with one query each for their determinations, evidence and filings. `evidence_to_dict`, `determination_to_dict` and `filing_to_dict` build the parts; `raw_item` is never returned.
- An Incident returns its stored `due_date` as YYYY-MM-DD or null, with `flags` computed on read (see Flags) and saved `filings` ordered oldest first by their internal `seq`. Its `evidence` is ordered by `imported_at`, then `evidence_id`; items from one import share an `imported_at` and their IDs are random, so they don't come back in the order they were sent.
- `workflow.audit_entry_to_dict(entry)` builds an audit entry (without `seq` or `session_id`) for the audit list and the snapshot.

## Filing request validation (`filings.py`; REQ-12, REQ-13, REQ-19)

- `validate_filing_request(body, *, today, determination_date, required_form)` checks an object and returns parsed fields without database access or changes to the input. The caller supplies the session date and the incident's determination date and required form; either incident value may be null. It performs no session, role, state or stale-version check.
- Checks run in this order: allowed filing form (`8-K Item 1.05`, `6-K`, `8-K/A`), filing date (real date, not after today or before a supplied determination date), accession number (10 ASCII digits, hyphen, 2 digits, hyphen, 6 digits), integer version at least 1, unavailable elements, home-disclosure date, then matching a supplied required form.
- `unavailable_elements` defaults to an empty list; sent values must be arrays of unique `nature`, `scope`, `timing` or `impact` names. Invalid items name their index. The result contains a separate list.
- `home_disclosure_date` is required for `6-K` and checked whenever sent: a real date no later than today. Otherwise its result is null. Returned dates are Python dates, `form_type` is a `FormType`, and `version` is an integer. Unknown request fields are ignored.

## Filing recording (`filings.py`; REQ-12, REQ-13, REQ-14, REQ-22)

- `record_filing` (DisclosureApprover only) follows the common error order, locks the incident row, and calls the validation helper with the session date and stored determination and required form. Stale versions are accepted.
- After body validation, only DeterminedMaterial, DisclosureDelayed and AmendmentPending allow filing; other states return 409 with no change.
- It appends a `Filing` with the parsed fields and real recording timestamp, sets DisclosureRecorded and adds 1 to the incident version. Determination, required form and due date are preserved. No EDGAR submission is performed.
- It writes one `record_filing` audit entry in the same transaction: `before: {state, version}`, `after: {state, version, filing}`. `filing` uses the public Filing schema, including nullable home-disclosure date, without internal `seq` or incident ID. Any failure rolls back the filing, incident change and audit together.
- Returns the full Incident, including all saved filings. The API returns 201; single reads and snapshots use the same formatter.

## Test control (`control.py`; TEST-01, TEST-02, TEST-06, TEST-07)

These take no identity headers or role and write no audit entries or project records. They raise `TracerError` like the rest, and the API layer turns it into `{message, source: "system"}` (see `api/AGENTS.md`).

- Each setting's value is checked by one function in `_CHECKS`, which setup and the settings update share, so both accept the same values: `test_now` (date-time), `registrant_type` (Domestic or FPI), `at_risk_threshold_days` (integer from 1 to `INTEGER_MAX`, 2,147,483,647) and `llm_base_url` (an absolute URI: a scheme, a colon, then something without whitespace, and valid under RFC 3986 by `rfc3986-validator`, so a malformed escape such as `%ZZ`, a character RFC 3986 doesn't allow, or a second `#` is a 400).
- `setup_session(body)` (`POST /test/setup`) checks the whole body before opening a transaction, so its 400s need no database:
  - `session_id` must be a non-blank string, and `setup` an object.
  - `setup` may hold only the four settings; any other key or a bad value is a 400 on `setup.<key>`. Unknown keys are checked first, then values in the order above.
  - A missing setting takes `DEFAULTS`: `test_now` 2026-10-05T15:00:00Z, Domestic, 7, and `https://llm.invalid/v1`.
  - It stores the session and returns `{session_id}`. An ID already in use is `CONFLICT` (409), including one stored by a concurrent setup after the existence check, caught on flush.
- `coverage_report(session_id)` (`GET /test/report?session_id=`) rejects a missing or blank `session_id` (400) before touching the database, then 404s an unknown session. It returns `{session_id, coverage: {code: {covered: null, total: null, basis}}}` with `basis` = `CODE_COVERAGE_BASIS`: code coverage isn't measured.
- `update_settings(session_id, body)` (`PATCH /test/sessions/{session_id}/settings`) changes an existing session's `MUTABLE_SETTINGS` (`test_now`, `at_risk_threshold_days`, `llm_base_url`). It checks the whole request before opening a transaction, so its 400s need no database and come before the 404:
  - A missing or blank `session_id` is a 400, as in `coverage_report`; a body that isn't an object is a 400 with no field.
  - Each key in body order: `registrant_type` is a 400 on `registrant_type` saying it's fixed for the session and can't be changed, whatever its value; any other key outside `MUTABLE_SETTINGS` is a 400 on that key ("Unknown setting '<key>'"). Then each value sent, in `MUTABLE_SETTINGS` order, through `_CHECKS`: a bad one is a 400 on `<key>`.
  - A setting left out keeps its value, so `{}` changes nothing and returns the settings as stored.
  - In one transaction it loads the session with `load_session(..., for_update=True)` (unknown, or an ID with NUL, → 404), sets the changed attributes and returns `run_to_dict(session)` (200). The ORM's UPDATE names only the attributes set, which are the columns the app role may update (see `scripts/AGENTS.md`); `{}` issues no UPDATE. Any error leaves the session as it was.
  - Every later date rule in the session uses the new `test_now` through `clock.today(session)`.
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

- It checks, in order: `decision` (Material or NotMaterial); `determination_date` (a date, then on or after `discovery_date` and not after today, both ends included, then, for a Domestic Material determination, a due date the holiday calendar can compute; see Deadline calendar); `rationale` (`require_string`); `evidence_refs` (a non-empty array of unique, non-blank strings without NUL, then every ID must belong to this incident, both on `evidence_refs`); `version`; then `voluntary_disclosure` if sent: a non-boolean is a 400, and `true` or `false` is a 501 `NOT_IMPLEMENTED`.
- After those, a state other than UnderAssessment is 409 `INVALID_TRANSITION`, so a second determination is a 409.
- Material sets `DeterminedMaterial` and `required_form` from the session: `8-K Item 1.05` for Domestic, `6-K` for FPI. NotMaterial sets `DeterminedNotMaterial` and `required_form: null`.
- Domestic Material sets `due_date` to four business days after `determination_date`; FPI and NotMaterial set it to null.
- It stores the determination with `voluntary_disclosure: false`, `recorded_by` = the user and `recorded_at` = server time, adds 1 to `version`, writes a `record_determination` audit entry (`before: {state, determination: null, version}`, `after: {state, determination: {decision, determination_date}, required_form, due_date, version}`) and returns the full Incident.

## Flags (`flags.py`; REQ-09)

`incident_flags` computes an Incident's `flags` on every read, from the session's `test_now`; nothing is stored, so moving the clock changes them with no write and no `version` change.

- `at_risk`: the incident is UnderAssessment and more than `at_risk_threshold_days` have passed since 00:00 UTC on `discovery_date`. Exactly the threshold isn't at risk. A determination clears it by leaving UnderAssessment.
- Due-date flags apply only with a `due_date`, in any state, DisclosureDelayed included:
  1. A filing whose `form_type` is the current `required_form` means no flag. An AmendmentPending incident never counts as filed, so an earlier 8-K/A filing doesn't clear a later amendment.
  2. Today after `due_date` is `overdue`.
  3. At most one business day after today, through `due_date`, is `warning`, so the due date itself is a `warning`.
- The count walks back from `due_date` and stops at its second business day, so a session clock outside the holiday calendar's range can't raise `OverflowError` on a read.
- `at_risk` comes before a due-date flag in the array.

## Deadline calendar (`deadlines.py`)

- Business days are Monday through Friday excluding U.S. federal holidays from the `holidays` library's `UnitedStates` calendar with `observed=True` and no state subdivision. Each cached year includes the following year's holidays before filtering, so New Year's Day observed in December is retained. Holiday dates and historical rules come from the library.
- `add_business_days(start, days=4)` excludes the start date, including a weekend or holiday start. Zero days returns the start unchanged. Calendar calculations do not depend on real time or session settings.
- Calendar years must be within the library's `start_year` and `end_year` (`MIN_CALENDAR_YEAR` and `MAX_CALENDAR_YEAR`). Unsupported starts, direct calendar queries and deadlines crossing the range raise `OverflowError`, which determination turns into a 400 on `determination_date` and amendment transition turns into a 400 on `info_available_date`. Both precede state checks; the determination error also precedes its `voluntary_disclosure` 501. The final supported year's calendar retains next New Year's observed holiday without requesting an unsupported year.
- `disclosure_due_date(required_form, start)` returns four business days after start for `8-K Item 1.05` and `8-K/A`, and null for other forms.

## State transitions (`workflow.py`; REQ-06)

`transition_incident` (DisclosureApprover only) applies only the pairs in `ALLOWED_TRANSITIONS`:

| From | To |
| --- | --- |
| DeterminedMaterial | DisclosureDelayed |
| DisclosureDelayed | DeterminedMaterial |
| DeterminedNotMaterial | Closed |
| DisclosureRecorded | AmendmentPending |
| DisclosureRecorded | Closed |

- It checks `to` (a `State` name: a non-string or unknown name is a 400 on `to`), then `version`, then `info_available_date` (a date not after today) and `elements` (a non-empty array, else a 400 on `elements`; each entry one of `DRAFT_ELEMENTS` (nature, scope, timing, impact) and not repeated, else a 400 on `elements[i]`).
- `info_available_date` and `elements` are required when `to` is AmendmentPending and checked whenever they're sent for any other target.
- For AmendmentPending, the latest filing is the one with the highest `seq`. When a filing exists, every requested element must be in its `unavailable_elements`; otherwise 400 on `elements[i]`, before the state check. A recorded filing is required to start an amendment; an incident with none returns 409 after body checks.
- A Domestic amendment requires `8-K/A`, due four business days after `info_available_date`; an FPI amendment requires `6-K` with a null due date. The existing calendar helper computes the deadline. A deadline outside its range is a 400 on `info_available_date`, before state checks. The availability date has no lower-bound check against earlier filing dates.
- After the body, any pair not in the table is 409 `INVALID_TRANSITION` with nothing changed. Determination and filing operations perform their own state changes; a direct transition never enters DisclosureRecorded. Closed has no outgoing transition.
- On success it sets `state`, adds 1 to `version`, writes a `transition` audit entry and returns the full Incident. Ordinary transitions preserve the determination, form and deadline and use `before: {state, version}`, `after: {state, version}`. Amendment requests also change form/deadline: both audit snapshots include `required_form` and `due_date`, and `after` also records `info_available_date` and the requested `elements`. Existing filings and determinations are never modified.

## Audit trail (`workflow.py`; REQ-22)

- Every successful create, import request, determination, transition and filing writes exactly one audit entry through `app/audit.py`, in the same transaction; rejected requests and GETs write none.
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
| `POST /incidents/{id}/drafts` | `not_built`, `REQUEST_DRAFT` | DisclosureApprover | incident | 501 |
| `POST /drafts/{draft_id}/accept` | `not_built`, `ACCEPT_DRAFT` | DisclosureApprover | none | 501 |

`voluntary_disclosure` on a determination is the other 501 (see Materiality determination).

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 checks, so the API layer must not reject a body before the service runs.
- Materiality is written only in `workflow.record_determination` (AC-09).
- Keep the allowed pairs in `ALLOWED_TRANSITIONS`; `transition_incident` checks only that set.
- An operation that ends in 405 or 501 goes in `placeholders.py`.
- Don't name a service function with a leading `test`: pytest would collect it from any test file that imports it. The snapshot's TestRun builder is `control.run_to_dict` for that reason.
