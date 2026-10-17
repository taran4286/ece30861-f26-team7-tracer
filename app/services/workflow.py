"""Phase 3 determinations, state transitions and audit retrieval.

Services return dictionaries; the future API layer handles headers and HTTP errors.
Materiality decisions belong to the determination operation.
"""

import re
from datetime import date
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.audit import audit
from app.config import RegistrantType, get_settings
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import (
    AuditAction, AuditEntry, Decision, Determination, Evidence, FormType, Incident, State,
)
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


def _invalid(field: str, message: str) -> NoReturn:
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


def record_determination(
    incident_id: str, body: Any, *, user_id: str, role: Role | str
) -> dict[str, Any]:
    """Record one determination and return the full incident as JSON-ready data.

    Identity headers belong to the future API layer. Service error order is role,
    incident existence, body validation, then state. Stale versions are accepted.
    """
    require_role(role, Operation.RECORD_DETERMINATION)
    with transaction() as tx:
        # Serialize competing determinations before reading state and version.
        incident = tx.scalar(
            select(Incident).where(Incident.incident_id == incident_id).with_for_update()
        )
        if incident is None:
            raise TracerError(ErrorCode.NOT_FOUND, "Incident not found")
        if not isinstance(body, dict):
            _invalid("body", "Expected an object")
        for field in ("decision", "determination_date", "rationale", "evidence_refs", "version"):
            if field not in body:
                _invalid(field, "Required field")
        if not isinstance(body["decision"], str) or body["decision"] not in {d.value for d in Decision}:
            _invalid("decision", "Expected Material or NotMaterial")
        decision = Decision(body["decision"])
        value = body["determination_date"]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            _invalid("determination_date", "Expected a YYYY-MM-DD date")
        try:
            determination_date = date.fromisoformat(value)
        except ValueError:
            _invalid("determination_date", "Expected a real calendar date")
        if not incident.discovery_date <= determination_date <= clock.today():
            _invalid("determination_date", "Date must be between discovery and today")
        rationale = body["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            _invalid("rationale", "Expected a non-empty string")
        refs = body["evidence_refs"]
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            _invalid("evidence_refs", "Expected at least one unique evidence ID")
        owned_refs = set(tx.scalars(select(Evidence.evidence_id).where(
            Evidence.incident_id == incident_id, Evidence.evidence_id.in_(refs)
        )))
        if owned_refs != set(refs):
            _invalid("evidence_refs", "Every evidence ID must belong to this incident")
        if type(body["version"]) is not int or body["version"] < 1:
            _invalid("version", "Expected an integer greater than or equal to 1")
        if "voluntary_disclosure" in body:
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
        evidence = tx.scalars(select(Evidence).where(Evidence.incident_id == incident_id)).all()
        return {
            "incident_id": incident_id, "title": incident.title,
            "discovery_date": incident.discovery_date.isoformat(), "state": incident.state.value,
            "required_form": incident.required_form.value if incident.required_form else None,
            "due_date": None, "flags": [], "filings": [], "group_id": incident.group_id,
            "version": incident.version,
            "determination": {
                "decision": decision.value, "determination_date": determination_date.isoformat(),
                "rationale": rationale, "evidence_refs": list(refs), "voluntary_disclosure": False,
                "recorded_by": user_id, "recorded_at": determination.recorded_at.isoformat(),
            },
            "evidence": [{
                "evidence_id": item.evidence_id, "incident_id": item.incident_id,
                "source": item.source.value, "source_ref": item.source_ref, "summary": item.summary,
                "observed_at": item.observed_at.isoformat(), "imported_by": item.imported_by,
                "imported_at": item.imported_at.isoformat(), "impact_categories": item.impact_categories,
            } for item in evidence],
        }
