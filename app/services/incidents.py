"""Create and get incidents (REQ-01, REQ-22), and the full Incident every operation returns."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.clock import today
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Determination, Evidence, Incident, State, new_id
from app.roles import Operation, Role, require_role
from app.services.validation import invalid, parse_date, require_object, require_string


def create_incident(body: Any, *, user_id: str, role: Role | str) -> dict[str, Any]:
    """POST /incidents: a new incident in UnderAssessment at version 1."""
    require_role(role, Operation.CREATE_INCIDENT)
    body = require_object(body)
    title = require_string(body, "title", "title")
    discovery_date = parse_date(body.get("discovery_date"), "discovery_date")
    if discovery_date > today():
        raise invalid("discovery_date", "discovery_date must not be after today")

    with transaction() as tx:
        incident = Incident(
            incident_id=new_id("inc"),
            title=title,
            discovery_date=discovery_date,
            state=State.UNDER_ASSESSMENT,
            required_form=None,
            due_date=None,
            group_id=None,
            version=1,
        )
        tx.add(incident)
        tx.flush()
        after = {
            "state": incident.state.value,
            "title": incident.title,
            "discovery_date": incident.discovery_date,
            "version": incident.version,
        }
        audit(tx, AuditAction.CREATE_INCIDENT, incident.incident_id, None, after, user_id=user_id)
        return incident_to_dict(tx, incident)


def get_incident(incident_id: str, *, user_id: str, role: Role | str) -> dict[str, Any]:
    """GET /incidents/{incident_id}: the full Incident, for every role."""
    require_role(role, Operation.READ)
    with transaction() as tx:
        return incident_to_dict(tx, load_incident(tx, incident_id))


def load_incident(tx: Session, incident_id: str, *, for_update: bool = False) -> Incident:
    """The incident named in the path, or NOT_FOUND."""
    incident = tx.get(Incident, incident_id, with_for_update=for_update)
    if incident is None:
        raise TracerError(ErrorCode.NOT_FOUND, f"Incident {incident_id!r} not found")
    return incident


def incident_to_dict(tx: Session, incident: Incident) -> dict[str, Any]:
    """The full Incident schema, JSON-ready. Every operation that returns an Incident uses this."""
    determination = tx.get(Determination, incident.incident_id)
    evidence = tx.scalars(
        select(Evidence)
        .where(Evidence.incident_id == incident.incident_id)
        .order_by(Evidence.imported_at, Evidence.evidence_id)
    ).all()
    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "discovery_date": incident.discovery_date.isoformat(),
        "state": incident.state.value,
        "determination": determination_to_dict(determination) if determination else None,
        "required_form": incident.required_form.value if incident.required_form else None,
        # Due dates, flags and filings are Phase 4.
        "due_date": None,
        "flags": [],
        "filings": [],
        "evidence": [evidence_to_dict(item) for item in evidence],
        "group_id": incident.group_id,
        "version": incident.version,
    }


def determination_to_dict(determination: Determination) -> dict[str, Any]:
    return {
        "decision": determination.decision.value,
        "determination_date": determination.determination_date.isoformat(),
        "rationale": determination.rationale,
        "evidence_refs": list(determination.evidence_refs),
        "voluntary_disclosure": determination.voluntary_disclosure,
        "recorded_by": determination.recorded_by,
        "recorded_at": determination.recorded_at.isoformat(),
    }


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    """The EvidenceItem schema; raw_item stays in the database for traceability."""
    return {
        "evidence_id": evidence.evidence_id,
        "source": evidence.source.value,
        "source_ref": evidence.source_ref,
        "summary": evidence.summary,
        "observed_at": evidence.observed_at.isoformat(),
        "imported_by": evidence.imported_by,
        "imported_at": evidence.imported_at.isoformat(),
        "impact_categories": list(evidence.impact_categories),
    }
