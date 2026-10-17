"""Phase 3 determinations, state transitions and audit retrieval."""

from datetime import date
from typing import Any

from sqlalchemy import select

from app import clock
from app.audit import audit
from app.config import RegistrantType, get_settings
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, AuditEntry, Decision, Determination, Evidence, FormType, State
from app.roles import Operation, Role, require_role
from app.services.incidents import incident_to_dict, load_incident
from app.services.validation import invalid, parse_date, require_object, require_string


# Allowed (current state, requested state) pairs from the Phase 3 guide.
# Determination and filing operations handle their own state changes.
ALLOWED_TRANSITIONS = frozenset(
    {
        (State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
        (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
        (State.DETERMINED_NOT_MATERIAL, State.CLOSED),
    }
)


def _transition_target(body: Any) -> State:
    """Check the request fields and return the requested state."""
    body = require_object(body)
    target = body.get("to")
    if not isinstance(target, str):
        raise invalid("to", "Expected a state name")
    try:
        # Check the state name here; check whether the move is allowed later.
        target = State(target)
    except ValueError:
        raise invalid("to", "Unknown state")
    version = body.get("version")
    # Exclude True/False, which Python otherwise treats as integers.
    # Stale versions are not rejected in Phase 3.
    if (
        isinstance(version, bool)
        or not (isinstance(version, int) or (isinstance(version, float) and version.is_integer()))
        or version < 1
    ):
        raise invalid("version", "Expected an integer greater than or equal to 1")
    if target == State.AMENDMENT_PENDING:
        # Validate the extra fields before rejecting this Phase 4 transition.
        # TODO: Match the pod contract's element names, uniqueness and date limit.
        value = body.get("info_available_date")
        if not isinstance(value, str):
            raise invalid("info_available_date", "Expected a YYYY-MM-DD date")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise invalid("info_available_date", "Expected a real YYYY-MM-DD date")
        # Require YYYY-MM-DD, not another date format accepted by Python.
        if parsed.isoformat() != value:
            raise invalid("info_available_date", "Expected a YYYY-MM-DD date")
        elements = body.get("elements")
        if not isinstance(elements, list) or not elements:
            raise invalid("elements", "Expected a non-empty array of element names")
        for index, element in enumerate(elements):
            if not isinstance(element, str) or not element.strip():
                raise invalid(f"elements[{index}]", "Expected a non-empty element name")
    return target


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
        incident = load_incident(tx, incident_id, for_update=True)
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
        return incident_to_dict(tx, incident)


def list_audit_entries(
    incident_id: str, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    """Return one incident's audit entries, oldest first. All roles may read.

    user_id keeps the common service signature; reads do not create audit entries.
    An unknown incident raises NOT_FOUND; an empty history returns {"entries": []}.
    """
    require_role(role, Operation.READ)
    with transaction() as tx:
        load_incident(tx, incident_id)
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


def record_determination(
    incident_id: str, body: Any, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    """Record one determination and return the full incident as JSON-ready data.

    Identity headers belong to the future API layer. Service error order is role,
    incident existence, body validation, then state. Stale versions are accepted.
    """
    require_role(role, Operation.RECORD_DETERMINATION)
    with transaction() as tx:
        incident = load_incident(tx, incident_id, for_update=True)
        body = require_object(body)
        value = body.get("decision")
        if not isinstance(value, str) or value not in {d.value for d in Decision}:
            raise invalid("decision", "Expected Material or NotMaterial")
        decision = Decision(value)
        determination_date = parse_date(body.get("determination_date"), "determination_date")
        if not incident.discovery_date <= determination_date <= clock.today():
            raise invalid("determination_date", "Date must be between discovery and today")
        rationale = require_string(body, "rationale", "rationale")
        refs = body.get("evidence_refs")
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() or "\x00" in ref for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise invalid("evidence_refs", "Expected at least one unique evidence ID")
        owned_refs = set(tx.scalars(select(Evidence.evidence_id).where(
            Evidence.incident_id == incident_id, Evidence.evidence_id.in_(refs)
        )))
        if owned_refs != set(refs):
            raise invalid("evidence_refs", "Every evidence ID must belong to this incident")
        version = body.get("version")
        if (
            isinstance(version, bool)
            or not (isinstance(version, int) or (isinstance(version, float) and version.is_integer()))
            or version < 1
        ):
            raise invalid("version", "Expected an integer greater than or equal to 1")
        if "voluntary_disclosure" in body:
            if not isinstance(body["voluntary_disclosure"], bool):
                raise invalid("voluntary_disclosure", "Expected a boolean")
            raise TracerError(ErrorCode.NOT_IMPLEMENTED, "Voluntary disclosure is not implemented")
        if incident.state != State.UNDER_ASSESSMENT:
            raise TracerError(ErrorCode.INVALID_TRANSITION, "Determination requires UnderAssessment")

        before = {"state": incident.state.value, "determination": None, "version": incident.version}
        determination = Determination(
            incident_id=incident_id, decision=decision, determination_date=determination_date,
            rationale=rationale, evidence_refs=list(refs), voluntary_disclosure=False,
            recorded_by=user_id, recorded_at=clock.utcnow(),
        )
        if decision == Decision.MATERIAL:
            incident.state = State.DETERMINED_MATERIAL
            incident.required_form = (
                FormType.FORM_6K if get_settings().registrant_type == RegistrantType.FPI
                else FormType.FORM_8K_ITEM_105
            )
        else:
            incident.state = State.DETERMINED_NOT_MATERIAL
            incident.required_form = None
        incident.version += 1
        tx.add(determination)
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, {
            "state": incident.state.value,
            "determination": {"decision": decision.value, "determination_date": determination_date.isoformat()},
            "required_form": incident.required_form.value if incident.required_form else None,
            "version": incident.version,
        }, user_id=user_id)
        return incident_to_dict(tx, incident)
