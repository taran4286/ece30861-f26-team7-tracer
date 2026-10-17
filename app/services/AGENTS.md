# app/services/: business logic

One module per area, callable without any web framework. The root `AGENTS.md` holds the rules for each endpoint, and its Cross-cutting rules and Audit trail sections cover transactions, audit entries and versions; this file covers how an operation is built.

| Module | Owner | Operations | Tests |
| --- | --- | --- | --- |
| `incidents.py` | Group B | Create and get an incident | `tests/test_incidents.py` |
| `evidence.py` | Group B | Import evidence | `tests/test_evidence.py` |
| `validation.py` | Group B | Shared body checks: `require_object`, `require_string`, `parse_date`, `parse_date_time`, `invalid` | Covered through the service tests |
| `workflow.py` | Group C | Record a determination, transitions, list audit entries | `tests/test_workflow.py` |

Add a row for each new module.

## Shape of every operation

Each operation takes the acting user's ID and role as keyword-only arguments (`*, user_id, role`) and runs these steps in this order, which gives the root file's error order:

1. `require_role(role, Operation.X)`: 403.
2. Load the incident, and any evidence named in the path: 404 if unknown.
3. Validate the whole body: 400 on the first bad field, before anything is written.
4. Check the state: 409 `INVALID_TRANSITION`.
5. Write the change and call `audit()` in the same `transaction()`. Any error raised inside it rolls back everything, including the audit entry.

```python
def record_determination(
    incident_id: str, body: Any, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    require_role(role, Operation.RECORD_DETERMINATION)
    with transaction() as tx:
        incident = load_incident(tx, incident_id, for_update=True)
        body = require_object(body)
        ...  # validate every field; raise invalid(field, message)
        if incident.state != State.UNDER_ASSESSMENT:
            raise TracerError(ErrorCode.INVALID_TRANSITION, ...)
        ...  # write
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, after, user_id=user_id)
        return incident_to_dict(tx, incident)
```

Header checks (root "Identity headers") come before step 1 and belong to the API layer.

## Shared helpers

- `incidents.load_incident(tx, incident_id, *, for_update=False)` loads the path incident or raises `NOT_FOUND`. Pass `for_update=True` when the operation changes the incident (determination, transition), so two concurrent requests can't both pass the state check or bump `version` from the same value.
- `incidents.incident_to_dict(tx, incident)` builds the full Incident schema (JSON-ready: dates and timestamps as ISO-8601 strings, enums as values). Every operation that returns an Incident uses it, so the shape stays in one place. `evidence_to_dict` and `determination_to_dict` build the EvidenceItem and Determination parts; `raw_item` is never returned.
- Validate bodies with `validation.py`, so dates and date-times are checked the same way everywhere: `parse_date` accepts only a real `YYYY-MM-DD`, and `parse_date_time` accepts only RFC 3339 with seconds and a `Z` or `±HH:MM` offset, returned in UTC.

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 check, so API request models must not reject a body before the service runs.
- Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- Materiality is written only in `workflow.record_determination`, and the allowed pairs live in `workflow.ALLOWED_TRANSITIONS` (root "Guardrails for REQ-08 (AC-09)" and "State machine and transitions"). `test_no_other_service_writes_materiality` scans every other module here, so a new module that writes the determination fails it.
- Evidence import validates items field by field in a fixed order (ticketing: `ticket_id`, `summary`, `created_at`; SIEM: `alert_id`, `rule_name`, `severity`, `detected_at`), then rejects the other source's keys listed in `evidence._FOREIGN_KEYS` (`items[i].<key>`): ticketing rejects `alert_id`, `rule_name`, `severity` and `detected_at`; SIEM rejects `ticket_id` and `created_at`. Other extra keys, including `summary` on a SIEM item, are kept in `raw_item`.
- Call `get_settings()` inside the operation, not at import time, so a test can switch the registrant type.
- `record_determination` returns 501 for `voluntary_disclosure` after body validation and before the state check; the root `AGENTS.md` Materiality determination section holds its other rules.
- `require_string` rejects missing, non-string and blank (whitespace-only) values, and NUL characters because PostgreSQL text cannot store them.
- `record_determination` and `transition_incident` accept a whole-number float `version` such as `1.0`, which JSON Schema 2020-12 counts as an integer.
