# app/services/: business logic

One module per area, callable without any web framework. The root `AGENTS.md` sections for each endpoint hold the exact rules; this file covers how every operation is built.

| Module | Owner | Operations | Tests |
| --- | --- | --- | --- |
| `incidents.py` | Group B | Create and get an incident | `tests/test_incidents.py` |
| `evidence.py` | Group B | Import evidence | `tests/test_evidence.py` |
| `workflow.py` | Group C | Record a determination, transitions, list audit entries | `tests/test_workflow.py` |

None of the modules exist yet; add each one's status here when it lands.

## Shape of every operation

Each operation takes the acting user's ID and role as arguments and runs these steps in this order, so errors come out in the order the root file requires:

1. `require_role(role, Operation.X)`: 403 before anything else.
2. Load the incident (and evidence, if referenced): 404 if unknown.
3. Validate the whole body: 400 on the first bad field, before anything is written.
4. Check the state: 409 `INVALID_TRANSITION`.
5. Write the change and call `audit()` in the same `transaction()`.

```python
def record_determination(incident_id: str, body: dict, *, user_id: str, role: Role | str):
    require_role(role, Operation.RECORD_DETERMINATION)
    with transaction() as tx:
        incident = ...  # 404 if missing
        ...  # validate every field; raise TracerError(VALIDATION_ERROR, ..., field=...)
        if incident.state is not State.UNDER_ASSESSMENT:
            raise TracerError(ErrorCode.INVALID_TRANSITION, ...)
        ...  # write
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, after, user_id=user_id)
```

Header checks (missing `X-User-Id` or `X-User-Role`) come before step 1 and belong to the API layer.

## Rules

- One `transaction()` per mutating operation, with its `audit()` call inside; never commit partway through. Any raised error rolls back everything, including the audit entry.
- Exactly one audit entry per successful create, import request, determination and transition; none for reads or rejected requests.
- `version` goes up by 1 on each determination and transition, never on import. Include it in `before` and `after`.
- Only the determination operation in `workflow.py` writes `Determination` rows or `required_form`. A test guards this (AC-09); keep it passing.
- Keep the allowed transitions as a data set in `workflow.py`, not as `if` chains.
- Read the registrant type from `get_settings().registrant_type` and dates from `clock`.
- Until import and determination are merged, group C tests insert incidents and evidence directly into the database.
