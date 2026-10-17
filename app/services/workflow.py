"""Phase 3 state transitions and audit retrieval (REQ-06, REQ-22).

Services return dictionaries; the future API layer handles headers and HTTP errors.
Materiality decisions belong to the determination operation.
"""

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, AuditEntry, Determination, Evidence, Incident, State
from app.roles import Operation, Role, require_role


# Allowed (current state, requested state) pairs from the Phase 3 guide.
# Determination and filing operations handle their own state changes.
ALLOWED_TRANSITIONS = frozenset(
    {
        (State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
        (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
        (State.DETERMINED_NOT_MATERIAL, State.CLOSED),
    }
)


def _invalid(field: str, message: str) -> None:
    """Raise a validation error naming the invalid field."""
    raise TracerError(ErrorCode.VALIDATION_ERROR, message, field=field)


def _transition_target(body: Any) -> State:
    """Check the request fields and return the requested state."""
    if not isinstance(body, dict):
        _invalid("body", "Expected an object")
    target = body.get("to")
    if not isinstance(target, str):
        _invalid("to", "Expected a state name")
    try:
        # Check the state name here; check whether the move is allowed later.
        target = State(target)
    except ValueError:
        _invalid("to", "Unknown state")
    version = body.get("version")
    # Exclude True/False, which Python otherwise treats as integers.
    # Stale versions are not rejected in Phase 3.
    if type(version) is not int or version < 1:
        _invalid("version", "Expected an integer greater than or equal to 1")
    if target == State.AMENDMENT_PENDING:
        # Validate the extra fields before rejecting this Phase 4 transition.
        # TODO: Match the pod contract's element names, uniqueness and date limit.
        value = body.get("info_available_date")
        if not isinstance(value, str):
            _invalid("info_available_date", "Expected a YYYY-MM-DD date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            _invalid("info_available_date", "Expected a real YYYY-MM-DD date")
        # Require YYYY-MM-DD, not another date format accepted by Python.
        if parsed.isoformat() != value:
            _invalid("info_available_date", "Expected a YYYY-MM-DD date")
        elements = body.get("elements")
        if not isinstance(elements, list) or not elements:
            _invalid("elements", "Expected a non-empty array of element names")
        for index, element in enumerate(elements):
            if not isinstance(element, str) or not element.strip():
                _invalid(f"elements[{index}]", "Expected a non-empty element name")
    return target


def _incident_view(tx: Session, incident: Incident) -> dict[str, Any]:
    """Build the incident response, including its existing determination and evidence.

    Use a shared response helper once the incident service provides one.
    """
    determination = tx.get(Determination, incident.incident_id)
    evidence = tx.scalars(
        select(Evidence)
        .where(Evidence.incident_id == incident.incident_id)
        # Keep a consistent order when import timestamps are equal.
        .order_by(Evidence.imported_at, Evidence.evidence_id)
    ).all()
    # Use ISO date strings and enum values in the response.
    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "discovery_date": incident.discovery_date.isoformat(),
        "state": incident.state.value,
        "determination": None if determination is None else {
            "decision": determination.decision.value,
            "determination_date": determination.determination_date.isoformat(),
            "rationale": determination.rationale,
            "evidence_refs": determination.evidence_refs,
            "voluntary_disclosure": determination.voluntary_disclosure,
            "recorded_by": determination.recorded_by,
            "recorded_at": determination.recorded_at.isoformat(),
        },
        "required_form": None if incident.required_form is None else incident.required_form.value,
        # Planned for Phase 4: deadlines, warning/overdue flags and filing records.
        # See root AGENTS.md "Scope" and Phase 2 Team 7 answers, Q6.2.
        # Phase 3 returns no deadline and empty lists, as the incident spec requires.
        "due_date": None,
        "flags": [],
        "filings": [],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "incident_id": item.incident_id,
                "source": item.source.value,
                "source_ref": item.source_ref,
                "summary": item.summary,
                "observed_at": item.observed_at.isoformat(),
                "imported_by": item.imported_by,
                "imported_at": item.imported_at.isoformat(),
                "impact_categories": item.impact_categories,
            }
            for item in evidence
        ],
        "group_id": incident.group_id,
        "version": incident.version,
    }


def transition_incident(
    incident_id: str, body: Any, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    """Apply a requested state change and return the updated incident.

    body contains `to` and `version`; user_id identifies the actor in the audit.
    Only DisclosureApprover may transition. The change and audit entry save together.
    """
    require_role(role, Operation.TRANSITION)
    # Roll back the operation if any step fails.
    with transaction() as tx:
        # Lock the incident so simultaneous requests cannot overwrite each other.
        incident = tx.get(Incident, incident_id, with_for_update=True)
        if incident is None:
            raise TracerError(ErrorCode.NOT_FOUND, "Incident not found")
        # Check existence before input, as required by the error order.
        target = _transition_target(body)
        if (incident.state, target) not in ALLOWED_TRANSITIONS:
            raise TracerError(
                ErrorCode.INVALID_TRANSITION,
                f"Cannot transition from {incident.state.value} to {target.value}",
            )
        # Record the old values. Only state and version change.
        before = {"state": incident.state.value, "version": incident.version}
        incident.state = target
        incident.version += 1
        after = {"state": incident.state.value, "version": incident.version}
        # Save one audit entry in the same transaction as the change.
        audit(tx, AuditAction.TRANSITION, incident_id, before, after, user_id=user_id)
        return _incident_view(tx, incident)


def list_audit_entries(
    incident_id: str, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    """Return one incident's audit entries, oldest first. All roles may read.

    user_id keeps the common service signature; reads do not create audit entries.
    An unknown incident raises NOT_FOUND; an empty history returns {"entries": []}.
    """
    require_role(role, Operation.READ)
    with transaction() as tx:
        if tx.get(Incident, incident_id) is None:
            raise TracerError(ErrorCode.NOT_FOUND, "Incident not found")
        # Include all actions for this incident. Order by sequence, since timestamps can tie.
        entries = tx.scalars(
            select(AuditEntry)
            .where(AuditEntry.target_id == incident_id)
            .order_by(AuditEntry.seq)
        ).all()
        # seq controls ordering but is not part of the response.
        return {
            "entries": [
                {
                    "entry_id": entry.entry_id,
                    "user_id": entry.user_id,
                    "timestamp": entry.timestamp.isoformat(),
                    "action": entry.action.value,
                    "target_id": entry.target_id,
                    "before": entry.before,
                    "after": entry.after,
                }
                for entry in entries
            ]
        }
