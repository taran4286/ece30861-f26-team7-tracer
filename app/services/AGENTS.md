# app/services/: business logic

One module per area, callable without any web framework. The root `AGENTS.md` holds the rules for each endpoint, and its Cross-cutting rules and Audit trail sections cover transactions, audit entries and versions; this file covers how an operation is built.

| Module | Operations | Tests |
| --- | --- | --- |
| `incidents.py` | Create and get an incident | `tests/test_incidents.py` |
| `evidence.py` | Import evidence | `tests/test_evidence.py` |
| `validation.py` | Shared body checks: `require_object`, `require_string`, `parse_integer`, `parse_date`, `parse_date_time`, `invalid` | Covered through the service tests |
| `workflow.py` | Record a determination, transitions, list audit entries | `tests/test_workflow.py` |
| `control.py` | Test control: set up a session, its coverage report, its snapshot | `tests/test_control.py` |
| `placeholders.py` | The 405 and 501 operations: `refuse_change` and `not_built` | `tests/test_placeholders.py` |

Add a row for each new module.

## Shape of every operation

Each operation takes one keyword-only argument per identity header, `*, user_id, role, session_id`, and runs these steps in this order inside one `transaction()`, which gives the root file's error order:

1. `load_session(tx, session_id)`: 404 for an unknown session.
2. `require_role(role, Operation.X)`: 403.
3. Load the incident with `load_incident(tx, incident_id, session_id=session_id)`, and any evidence named in the path: 404 if unknown or in another session.
4. Validate the whole body: 400 on the first bad field, before anything is written. Date rules compare against `clock.today(session)`.
5. Check the state: 409 `INVALID_TRANSITION`.
6. Write the change and call `audit(..., session_id=session_id)`. Any error raised inside the transaction rolls back everything, including the audit entry.

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

Header checks (root "Identity headers") come before step 1 and belong to the API layer.

## Shared helpers

- `incidents.load_incident(tx, incident_id, *, session_id, for_update=False)` loads the path incident or raises `NOT_FOUND`, treating another session's incident as unknown. Evidence and determinations need no session check of their own: they're reached through an incident already loaded this way. Pass `for_update=True` when the operation changes the incident (determination, transition), so two concurrent requests can't both pass the state check or bump `version` from the same value.
- `evidence.load_evidence(tx, incident_id, evidence_id)` loads evidence named in the path, after its incident, or raises `NOT_FOUND`; evidence of another incident counts as unknown.
- `incidents.incident_to_dict(tx, incident)` builds the full Incident schema (JSON-ready: dates and timestamps as ISO-8601 strings, enums as values). Every operation that returns an Incident uses it, so the shape stays in one place. `incidents_to_dicts(tx, incidents)` builds many at once with one query each for their determinations and evidence; `incident_to_dict` is that for one incident. `evidence_to_dict` and `determination_to_dict` build the EvidenceItem and Determination parts; `raw_item` is never returned.
- `workflow.audit_entry_to_dict(entry)` builds the AuditEntry schema for the audit list and the snapshot.
- Validate bodies with `validation.py`, so dates and date-times are checked the same way everywhere: `parse_date` accepts only a real `YYYY-MM-DD`, and `parse_date_time` accepts only RFC 3339 with seconds and a `Z` or `±HH:MM` offset, returned in UTC.

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 check, so API request models must not reject a body before the service runs.
- Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- Materiality is written only in `workflow.record_determination`, and the allowed pairs live in `workflow.ALLOWED_TRANSITIONS` (root "Guardrails for REQ-08 (AC-09)" and "State machine and transitions"). `test_no_other_service_writes_materiality` scans every other module here, so a new module that writes the determination fails it.
- Evidence import validates items field by field in a fixed order (ticketing: `ticket_id`, `summary`, `created_at`; SIEM: `alert_id`, `rule_name`, `severity`, `detected_at`), then rejects the other source's keys listed in `evidence._FOREIGN_KEYS` (`items[i].<key>`): ticketing rejects `alert_id`, `rule_name`, `severity` and `detected_at`; SIEM rejects `ticket_id` and `created_at`. Other extra keys, including `summary` on a SIEM item, are kept in `raw_item`.
- Take today and the registrant type from the loaded session (`clock.today(session)`, `session.registrant_type`), never from config or the real date.
- `record_determination` returns 501 for `voluntary_disclosure` after body validation and before the state check; the root `AGENTS.md` Materiality determination section holds its other rules.
- `require_string` rejects missing, non-string and blank (whitespace-only) values, and NUL characters because PostgreSQL text cannot store them.
- Check integers with `parse_integer`: it accepts a whole-number float such as `1.0`, which JSON Schema 2020-12 counts as an integer, and rejects booleans. Pass `maximum=control.INTEGER_MAX` for a value stored in an `Integer` column, so a value too large to store is a 400, not a failed insert.
- `transition_incident` checks `info_available_date` and `elements` whenever they're sent, against the names in `workflow.DRAFT_ELEMENTS`, and requires them only for AmendmentPending.
- An operation that ends in 405 or 501 goes in `placeholders.py`, which runs steps 1 to 3 before raising, so error order holds; it writes nothing.
- `control.py` is the exception to the shape above: test control takes no identity headers or role and writes no audit entries. It raises `TracerError` like the rest (`CONFLICT` for a session ID in use), and the API layer turns that into a `TestControlError`.
- Don't name a service function with a leading `test`: pytest would collect it from any test file that imports it. The snapshot's TestRun builder is `control.run_to_dict` for that reason.
