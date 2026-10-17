# app/services/: business logic

One module per area, callable without any web framework. The root `AGENTS.md` holds the rules for each endpoint, and its Cross-cutting rules and Audit trail sections cover transactions, audit entries and versions; this file covers how an operation is built.

| Module | Owner | Operations | Tests |
| --- | --- | --- | --- |
| `incidents.py` | Group B | Create and get an incident | `tests/test_incidents.py` |
| `evidence.py` | Group B | Import evidence | `tests/test_evidence.py` |
| `validation.py` | Group B | Shared body checks: `require_object`, `require_string`, `parse_date`, `parse_date_time`, `invalid` | Covered through the service tests |
| `workflow.py` | Group C | Record a determination, transitions, list audit entries | `tests/test_workflow.py` |

`incidents.py` and `evidence.py` implement incident creation, retrieval and evidence
import. `workflow.py` implements determination recording, state transitions and
audit retrieval. Workflow tests remain to be added.

`ALLOWED_TRANSITIONS` contains the three Phase 3 transition pairs. The transition
operation locks the incident, validates the raw body, updates state and version,
and records its audit entry in one transaction. The supplied version is validated
but not compared with the stored version. Its response includes the existing
determination and evidence; neither is changed. Incident response assembly is
local to workflow until the incident service supplies a shared response helper.

For the disallowed `AmendmentPending` target, validation currently requires a real
`YYYY-MM-DD` `info_available_date` and a non-empty array of non-empty strings in
`elements`, before the state check. Element names await the pod's final API schema;
no Phase 4 amendment behavior is implemented.

Audit retrieval checks the incident exists, filters by its target ID and orders
by `seq`. It returns `{"entries": [...]}` without exposing the internal sequence
column and without adding an audit entry for the read. HTTP header checks, routes,
and 405/501 responses belong to the future API layer.

## Shape of every operation

Each operation takes the acting user's ID and role as arguments and runs these steps in this order, which gives the root file's error order:

1. `require_role(role, Operation.X)`: 403.
2. Load the incident, and any evidence named in the path: 404 if unknown.
3. Validate the whole body: 400 on the first bad field, before anything is written.
4. Check the state: 409 `INVALID_TRANSITION`.
5. Write the change and call `audit()` in the same `transaction()`. Any error raised inside it rolls back everything, including the audit entry.

```python
def record_determination(incident_id: str, body: dict, *, user_id: str, role: Role | str):
    require_role(role, Operation.RECORD_DETERMINATION)
    with transaction() as tx:
        incident = tx.get(Incident, incident_id, with_for_update=True)
        if incident is None:
            raise TracerError(ErrorCode.NOT_FOUND, ...)
        ...  # validate every field; raise TracerError(ErrorCode.VALIDATION_ERROR, ..., field=...)
        if incident.state != State.UNDER_ASSESSMENT:
            raise TracerError(ErrorCode.INVALID_TRANSITION, ...)
        ...  # write
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, after, user_id=user_id)
```

Header checks (missing `X-User-Id` or `X-User-Role`) come before step 1 and belong to the API layer.

## Shared helpers

- `incidents.load_incident(tx, incident_id, for_update=False)` loads the path incident or raises `NOT_FOUND`; pass `for_update=True` when the operation changes the incident.
- `incidents.incident_to_dict(tx, incident)` builds the full Incident schema (JSON-ready: dates and timestamps as ISO-8601 strings, enums as values). Every operation that returns an Incident uses it, so the shape stays in one place.
- Validate bodies with `validation.py`, so dates and date-times are checked the same way everywhere: `parse_date` accepts only a real `YYYY-MM-DD`, and `parse_date_time` accepts only RFC 3339 with seconds and a `Z` or `±HH:MM` offset, returned in UTC.

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 check, so API request models must not reject a body before the service runs.
- Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- When an operation changes the incident (determination, transition), load it with `with_for_update=True`, so two concurrent requests can't both pass the state check or bump `version` from the same value.
- Keep materiality writes in `workflow.py`'s determination operation, and the allowed transitions as a data set in `workflow.py`.
- Evidence import validates items field by field in a fixed order (ticketing: `ticket_id`, `summary`, `created_at`; SIEM: `alert_id`, `rule_name`, `severity`, `detected_at`), then rejects any key only the other source sends (`items[i].<key>`). Other extra keys are allowed and kept in `raw_item`.
- Call `get_settings()` inside the operation, not at import time, so a test can switch the registrant type.

## Materiality determination implementation

`workflow.record_determination(incident_id, body, *, user_id, role)` implements the Phase 3 determination operation. It locks the incident before checking its state, validates the required fields and incident-owned evidence references, and writes the determination, incident update and audit entry in one transaction. Only a DisclosureApprover may call it successfully, and only from UnderAssessment.

Material decisions select `8-K Item 1.05` for Domestic registrants or `6-K` for FPI registrants; NotMaterial leaves `required_form` null. A successful determination increments the stored version, while a valid stale request version is accepted in Phase 3. Any supplied `voluntary_disclosure` value, including false or null, raises `NOT_IMPLEMENTED` after required body validation and before the state check.

The operation currently builds its JSON-ready Incident response locally. Consolidate this with `incidents.incident_to_dict` when integrating the services. HTTP routing and identity-header validation remain API-layer work. Determination behavior tests and the guardrail ensuring other handlers never write materiality are deferred and must be added before Phase 3 is considered complete.
