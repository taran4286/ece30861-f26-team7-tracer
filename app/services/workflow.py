"""Determinations, disclosure deadlines, state transitions and audit retrieval."""

from datetime import date
from typing import Any

from sqlalchemy import select

from app import clock
from app.audit import audit
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import (
    AuditAction,
    AuditEntry,
    Decision,
    Determination,
    Evidence,
    FormType,
    RegistrantType,
    State,
)
from app.roles import Operation, Role, require_role
from app.services.deadlines import disclosure_due_date
from app.services.incidents import incident_to_dict, load_incident
from app.services.validation import (
    invalid,
    parse_date,
    parse_integer,
    require_object,
    require_string,
)
from app.sessions import load_session


# Allowed (current state, requested state) pairs; every other pair is a 409.
# Determination and filing operations handle their own state changes.
ALLOWED_TRANSITIONS = frozenset(
    {
        (State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED),
        (State.DISCLOSURE_DELAYED, State.DETERMINED_MATERIAL),
        (State.DETERMINED_NOT_MATERIAL, State.CLOSED),
    }
)

# The spec's DraftElementName values, which AmendmentPending's `elements` must come from.
DRAFT_ELEMENTS = ("nature", "scope", "timing", "impact")


def _transition_target(body: Any, today: date) -> State:
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
    # Stale versions are accepted.
    parse_integer(body.get("version"), "version")
    # Required for AmendmentPending, and checked whenever sent, since the request schema types
    # them for every target. Validated before this Phase 4 transition is refused.
    amendment = target == State.AMENDMENT_PENDING
    if amendment or "info_available_date" in body:
        if parse_date(body.get("info_available_date"), "info_available_date") > today:
            raise invalid("info_available_date", "info_available_date must not be after today")
    if amendment or "elements" in body:
        _check_elements(body.get("elements"))
    return target


def _check_elements(elements: Any) -> None:
    """A non-empty array of unique DraftElementName values."""
    if not isinstance(elements, list) or not elements:
        raise invalid("elements", "Expected a non-empty array of element names")
    for index, element in enumerate(elements):
        if not isinstance(element, str) or element not in DRAFT_ELEMENTS:
            allowed = ", ".join(DRAFT_ELEMENTS)
            raise invalid(f"elements[{index}]", f"Expected one of: {allowed}")
        if element in elements[:index]:
            raise invalid(f"elements[{index}]", f"{element} appears more than once")


def transition_incident(
    incident_id: str, body: Any, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """Apply a requested state change and return the updated incident.

    body contains `to` and `version`; user_id identifies the actor in the audit.
    Only DisclosureApprover may transition. The change and audit entry save together.
    """
    # Roll back the operation if any step fails.
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.TRANSITION)
        # Lock the incident so simultaneous requests cannot overwrite each other.
        incident = load_incident(tx, incident_id, session_id=session_id, for_update=True)
        # Check existence before input, as required by the error order.
        target = _transition_target(body, clock.today(session))
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
        audit(
            tx, AuditAction.TRANSITION, incident_id, before, after,
            user_id=user_id, session_id=session_id,
        )
        return incident_to_dict(tx, incident)


def list_audit_entries(
    incident_id: str, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """Return one incident's audit entries, oldest first. All roles may read.

    user_id keeps the common service signature; reads do not create audit entries.
    An unknown incident raises NOT_FOUND; an empty history returns {"entries": []}.
    """
    with transaction() as tx:
        load_session(tx, session_id)
        require_role(role, Operation.READ)
        load_incident(tx, incident_id, session_id=session_id)
        # Include all actions for this incident. Order by sequence, since timestamps can tie.
        entries = tx.scalars(
            select(AuditEntry)
            .where(AuditEntry.target_id == incident_id)
            .order_by(AuditEntry.seq)
        ).all()
        return {"entries": [audit_entry_to_dict(entry) for entry in entries]}


def audit_entry_to_dict(entry: AuditEntry) -> dict[str, Any]:
    """The AuditEntry schema. seq sets the order but isn't part of it, and neither is session_id."""
    return {
        "entry_id": entry.entry_id,
        "user_id": entry.user_id,
        "timestamp": entry.timestamp.isoformat(),
        "action": entry.action.value,
        "target_id": entry.target_id,
        "before": entry.before,
        "after": entry.after,
    }


def record_determination(
    incident_id: str, body: Any, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """Record one determination and return the full incident as JSON-ready data.

    The API layer checks the identity headers first. Service error order is session,
    role, incident existence, body validation, then state. Stale versions are accepted.
    """
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.RECORD_DETERMINATION)
        incident = load_incident(tx, incident_id, session_id=session_id, for_update=True)
        body = require_object(body)
        value = body.get("decision")
        if not isinstance(value, str) or value not in {d.value for d in Decision}:
            raise invalid("decision", "Expected Material or NotMaterial")
        decision = Decision(value)
        determination_date = parse_date(body.get("determination_date"), "determination_date")
        if not incident.discovery_date <= determination_date <= clock.today(session):
            raise invalid("determination_date", "Date must be between discovery and today")
        required_form = None
        if decision == Decision.MATERIAL:
            required_form = (
                FormType.FORM_6K if session.registrant_type == RegistrantType.FPI
                else FormType.FORM_8K_ITEM_105
            )
        try:
            due_date = disclosure_due_date(required_form, determination_date)
        except OverflowError:
            raise invalid("determination_date", "Deadline is outside the supported holiday calendar range") from None
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
        parse_integer(body.get("version"), "version")
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
        else:
            incident.state = State.DETERMINED_NOT_MATERIAL
        incident.required_form = required_form
        incident.due_date = due_date
        incident.version += 1
        tx.add(determination)
        audit(tx, AuditAction.RECORD_DETERMINATION, incident_id, before, {
            "state": incident.state.value,
            "determination": {"decision": decision.value, "determination_date": determination_date.isoformat()},
            "required_form": incident.required_form.value if incident.required_form else None,
            "due_date": due_date.isoformat() if due_date else None,
            "version": incident.version,
        }, user_id=user_id, session_id=session_id)
        return incident_to_dict(tx, incident)
