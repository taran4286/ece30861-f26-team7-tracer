"""Create and get incidents (REQ-01, REQ-22), and the full Incident every operation returns."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.clock import today
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Determination, Evidence, Filing, Incident, State, new_id
from app.roles import Operation, Role, require_role
from app.services.validation import invalid, parse_date, require_object, require_string
from app.sessions import load_session


def create_incident(
    body: Any, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """POST /incidents: a new incident in UnderAssessment at version 1."""
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.CREATE_INCIDENT)
        body = require_object(body)
        title = require_string(body, "title", "title")
        discovery_date = parse_date(body.get("discovery_date"), "discovery_date")
        if discovery_date > today(session):
            raise invalid("discovery_date", "discovery_date must not be after today")

        incident = Incident(
            incident_id=new_id("inc"),
            session_id=session_id,
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
        audit(
            tx, AuditAction.CREATE_INCIDENT, incident.incident_id, None, after,
            user_id=user_id, session_id=session_id,
        )
        return incident_to_dict(tx, incident)


def get_incident(
    incident_id: str, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """GET /incidents/{incident_id}: the full Incident, for every role."""
    with transaction() as tx:
        load_session(tx, session_id)
        require_role(role, Operation.READ)
        return incident_to_dict(tx, load_incident(tx, incident_id, session_id=session_id))


def load_incident(
    tx: Session, incident_id: str, *, session_id: str, for_update: bool = False
) -> Incident:
    """The incident named in the path, or NOT_FOUND; another session's incident counts as unknown."""
    # PostgreSQL text can't hold NUL, so no stored ID has one, and querying for it would fail.
    incident = None if "\x00" in incident_id else tx.get(
        Incident, incident_id, with_for_update=for_update
    )
    if incident is None or incident.session_id != session_id:
        raise TracerError(ErrorCode.NOT_FOUND, f"Incident {incident_id!r} not found")
    return incident


def incident_to_dict(tx: Session, incident: Incident) -> dict[str, Any]:
    """The full Incident schema, JSON-ready. Every operation that returns an Incident uses this."""
    [result] = incidents_to_dicts(tx, [incident])
    return result


def incidents_to_dicts(tx: Session, incidents: Sequence[Incident]) -> list[dict[str, Any]]:
    """Build many incidents with one query each for determinations, evidence and filings."""
    ids = [incident.incident_id for incident in incidents]
    determinations = {
        row.incident_id: row
        for row in tx.scalars(select(Determination).where(Determination.incident_id.in_(ids)))
    }
    evidence: dict[str, list[Evidence]] = {incident_id: [] for incident_id in ids}
    for item in tx.scalars(
        select(Evidence)
        .where(Evidence.incident_id.in_(ids))
        .order_by(Evidence.imported_at, Evidence.evidence_id)
    ):
        evidence[item.incident_id].append(item)
    filings: dict[str, list[Filing]] = {incident_id: [] for incident_id in ids}
    for filing in tx.scalars(
        select(Filing).where(Filing.incident_id.in_(ids)).order_by(Filing.seq)
    ):
        filings[filing.incident_id].append(filing)
    return [
        _incident_dict(incident, determinations.get(incident.incident_id),
                       evidence[incident.incident_id], filings[incident.incident_id])
        for incident in incidents
    ]


def _incident_dict(
    incident: Incident, determination: Determination | None, evidence: list[Evidence],
    filings: list[Filing],
) -> dict[str, Any]:
    """Build the public incident response, including its stored deadline when present."""
    return {
        "incident_id": incident.incident_id,
        "title": incident.title,
        "discovery_date": incident.discovery_date.isoformat(),
        "state": incident.state.value,
        "determination": determination_to_dict(determination) if determination else None,
        "required_form": incident.required_form.value if incident.required_form else None,
        "due_date": incident.due_date.isoformat() if incident.due_date else None,
        "flags": [],
        "filings": [filing_to_dict(filing) for filing in filings],
        "evidence": [evidence_to_dict(item) for item in evidence],
        "group_id": incident.group_id,
        "version": incident.version,
    }


def filing_to_dict(filing: Filing) -> dict[str, Any]:
    """The public Filing schema; the internal sequence and incident ID stay out."""
    return {
        "form_type": filing.form_type.value,
        "filing_date": filing.filing_date.isoformat(),
        "accession_number": filing.accession_number,
        "unavailable_elements": list(filing.unavailable_elements),
        "home_disclosure_date": (
            filing.home_disclosure_date.isoformat() if filing.home_disclosure_date else None
        ),
        "recorded_by": filing.recorded_by,
        "recorded_at": filing.recorded_at.isoformat(),
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
        "incident_id": evidence.incident_id,
        "source": evidence.source.value,
        "source_ref": evidence.source_ref,
        "summary": evidence.summary,
        "observed_at": evidence.observed_at.isoformat(),
        "imported_by": evidence.imported_by,
        "imported_at": evidence.imported_at.isoformat(),
        "impact_categories": list(evidence.impact_categories),
    }
