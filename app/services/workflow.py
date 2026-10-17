"""Materiality determinations; HTTP handling is added after API confirmation."""

from typing import Any

from sqlalchemy import select

from app import clock
from app.audit import audit
from app.config import RegistrantType, get_settings
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Decision, Determination, Evidence, FormType, State
from app.roles import Operation, Role, require_role
from app.services.incidents import incident_to_dict, load_incident
from app.services.validation import invalid, parse_date, require_object, require_string


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
