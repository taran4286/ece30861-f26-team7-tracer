"""Materiality determinations; HTTP handling is added after API confirmation."""

import re
from datetime import date
from typing import Any, NoReturn

from sqlalchemy import select

from app import clock
from app.audit import audit
from app.config import RegistrantType, get_settings
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Decision, Determination, Evidence, FormType, Incident, State
from app.roles import Operation, Role, require_role


def _invalid(field: str, message: str) -> NoReturn:
    raise TracerError(ErrorCode.VALIDATION_ERROR, message, field)


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
