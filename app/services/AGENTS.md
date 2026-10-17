# app/services/: business logic

One module per area, callable without any web framework. The root `AGENTS.md` holds the rules for each endpoint, and its Cross-cutting rules and Audit trail sections cover transactions, audit entries and versions; this file covers how an operation is built.

| Module | Owner | Operations | Tests |
| --- | --- | --- | --- |
| `incidents.py` | Group B | Create and get an incident | `tests/test_incidents.py` |
| `evidence.py` | Group B | Import evidence | `tests/test_evidence.py` |
| `workflow.py` | Group C | Record a determination, transitions, list audit entries | `tests/test_workflow.py` |

`incidents.py`, `evidence.py`, determination recording and the listed test files
are planned. `workflow.py` provides `transition_incident(incident_id, body, *,
user_id, role)` and `list_audit_entries(incident_id, *, user_id, role)`. Both return
plain response dictionaries with ISO-formatted dates and enum values, ready for
the future API layer. Add a row for any new module.

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

## Rules

- Services take the raw request body and do all body validation themselves. A 400 must come after the 404 check, so API request models must not reject a body before the service runs.
- Unknown IDs inside a body, like `evidence_refs`, are 400s on that field; only IDs in the path are 404s.
- When an operation changes the incident (determination, transition), load it with `with_for_update=True`, so two concurrent requests can't both pass the state check or bump `version` from the same value.
- Keep materiality writes in `workflow.py`'s determination operation, and the allowed transitions as a data set in `workflow.py`.
- Call `get_settings()` inside the operation, not at import time, so a test can switch the registrant type.
