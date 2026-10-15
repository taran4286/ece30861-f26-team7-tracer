"""Import simulated ticketing and SIEM items as normalized evidence (REQ-02, REQ-03)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.audit import audit
from app.clock import utcnow
from app.db import transaction
from app.models import AuditAction, Evidence, EvidenceSource, SiemSeverity, new_id
from app.roles import Operation, Role, require_role
from app.services.incidents import evidence_to_dict, load_incident
from app.services.validation import (
    invalid,
    parse_date_time,
    require_object,
    require_string,
)


@dataclass(frozen=True)
class _ItemShape:
    """Where each normalized field comes from in one source's items."""

    ref_key: str
    summary_key: str
    time_key: str


SHAPES: dict[EvidenceSource, _ItemShape] = {
    EvidenceSource.TICKETING: _ItemShape("ticket_id", "summary", "created_at"),
    EvidenceSource.SIEM: _ItemShape("alert_id", "rule_name", "detected_at"),
}

# Keys only the other source sends; an item carrying one is shaped for the wrong source.
_FOREIGN_KEYS: dict[EvidenceSource, tuple[str, ...]] = {
    EvidenceSource.TICKETING: ("alert_id", "rule_name", "severity", "detected_at"),
    EvidenceSource.SIEM: ("ticket_id", "created_at"),
}


@dataclass(frozen=True)
class _NormalizedItem:
    source_ref: str
    summary: str
    observed_at: datetime
    raw_item: dict[str, Any]


def import_evidence(
    incident_id: str, body: Any, *, user_id: str, role: Role | str
) -> dict[str, list[dict[str, Any]]]:
    """POST /incidents/{incident_id}/evidence/import: all items or none, in any state."""
    require_role(role, Operation.IMPORT_EVIDENCE)
    with transaction() as tx:
        load_incident(tx, incident_id)
        source, items = _validate(body)

        imported_at = utcnow()
        rows = [
            Evidence(
                evidence_id=new_id("ev"),
                incident_id=incident_id,
                source=source,
                source_ref=item.source_ref,
                summary=item.summary,
                observed_at=item.observed_at,
                imported_by=user_id,
                imported_at=imported_at,
                impact_categories=[],
                raw_item=item.raw_item,
            )
            for item in items
        ]
        tx.add_all(rows)
        tx.flush()
        # Import never changes the incident's version, so it isn't in the entry.
        after = {"source": source.value, "evidence_ids": [row.evidence_id for row in rows]}
        audit(tx, AuditAction.IMPORT_EVIDENCE, incident_id, None, after, user_id=user_id)
        return {"evidence": [evidence_to_dict(row) for row in rows]}


def _validate(body: Any) -> tuple[EvidenceSource, list[_NormalizedItem]]:
    """Validate the whole ImportRequest, raising on the first bad field."""
    body = require_object(body)
    try:
        source = EvidenceSource(body.get("source"))
    except ValueError:
        allowed = ", ".join(s.value for s in EvidenceSource)
        raise invalid("source", f"source must be one of: {allowed}") from None

    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise invalid("items", "items must be a non-empty array")
    return source, [_normalize(source, item, f"items[{i}]") for i, item in enumerate(items)]


def _normalize(source: EvidenceSource, item: Any, path: str) -> _NormalizedItem:
    item = require_object(item, path)
    shape = SHAPES[source]
    source_ref = require_string(item, shape.ref_key, f"{path}.{shape.ref_key}")
    summary = require_string(item, shape.summary_key, f"{path}.{shape.summary_key}")
    if source is EvidenceSource.SIEM:
        severity = item.get("severity")
        if not isinstance(severity, str) or severity not in {s.value for s in SiemSeverity}:
            allowed = ", ".join(s.value for s in SiemSeverity)
            raise invalid(f"{path}.severity", f"{path}.severity must be one of: {allowed}")
    observed_at = parse_date_time(item.get(shape.time_key), f"{path}.{shape.time_key}")

    for key in _FOREIGN_KEYS[source]:
        if key in item:
            raise invalid(f"{path}.{key}", f"{path} is a {_other(source)} item, not {source}")
    return _NormalizedItem(source_ref, summary, observed_at, item)


def _other(source: EvidenceSource) -> str:
    return next(s.value for s in EvidenceSource if s is not source)
