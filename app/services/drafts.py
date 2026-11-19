"""Read the inputs for a draft, validate its evidence citations, and accept a stored draft."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.clock import utcnow
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Draft, DraftStatus, Evidence, Incident, State
from app.roles import Operation, Role, require_role
from app.services.incidents import evidence_to_dict, load_incident
from app.services.validation import invalid, require_object, require_string
from app.services.workflow import DRAFT_ELEMENTS
from app.sessions import load_session


_DRAFT_STATES = frozenset({
    State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED, State.AMENDMENT_PENDING,
})
_UNDETERMINED = "Not yet determined"


def load_draft_context(
    incident_id: str, *, role: Role | str, session_id: str,
) -> dict[str, Any]:
    """Read the eligible incident and its evidence before calling a draft provider."""
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.REQUEST_DRAFT)
        incident = load_incident(tx, incident_id, session_id=session_id)
        if incident.state in {State.UNDER_ASSESSMENT, State.DETERMINED_NOT_MATERIAL}:
            raise TracerError(ErrorCode.NOT_MATERIAL, "A draft requires a material incident")
        if incident.state not in _DRAFT_STATES:
            raise TracerError(ErrorCode.INVALID_TRANSITION, "The incident state does not allow drafting")
        evidence = tx.scalars(
            select(Evidence)
            .where(Evidence.incident_id == incident_id)
            .order_by(Evidence.imported_at, Evidence.evidence_id)
        )
        return {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "discovery_date": incident.discovery_date.isoformat(),
            "form_type": incident.required_form.value,
            "llm_base_url": session.llm_base_url,
            "evidence": [evidence_to_dict(item) for item in evidence],
        }


def validate_draft_elements(elements: Any, *, evidence_ids: set[str]) -> None:
    """Check the provider's four normalized elements against this incident's evidence IDs.

    Raise a field-specific error for the provider caller to handle. Evidence ownership is
    checkable here. Whether the evidence supports the prose is a separate review.
    """
    elements = require_object(elements, "elements")
    if elements.keys() != set(DRAFT_ELEMENTS):
        raise invalid("elements", "Expected exactly nature, scope, timing and impact")

    for name in DRAFT_ELEMENTS:
        field = f"elements.{name}"
        element = require_object(elements[name], field)
        if element.keys() != {"text", "citations", "sensitive_flags"}:
            raise invalid(field, "Expected exactly text, citations and sensitive_flags")
        text = require_string(element, "text", f"{field}.text")
        citations = element["citations"]
        flags = element["sensitive_flags"]
        if not isinstance(citations, list):
            raise invalid(f"{field}.citations", "Expected a list of evidence IDs")
        if not isinstance(flags, list):
            raise invalid(f"{field}.sensitive_flags", "Expected a list of flags")

        if text == _UNDETERMINED:
            if citations or flags:
                raise invalid(field, "Not yet determined requires empty citations and flags")
            continue

        if not citations:
            raise invalid(f"{field}.citations", "Draft text requires at least one citation")
        for index, citation in enumerate(citations):
            if not isinstance(citation, str) or citation not in evidence_ids:
                raise invalid(f"{field}.citations[{index}]", "Citation must reference this incident's evidence")

        for index, flag in enumerate(flags):
            flag_field = f"{field}.sensitive_flags[{index}]"
            flag = require_object(flag, flag_field)
            require_string(flag, "excerpt", f"{flag_field}.excerpt")
            require_string(flag, "reason", f"{flag_field}.reason")


def accept_draft(
    draft_id: str, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """POST /drafts/{draft_id}/accept: mark a Proposed draft Accepted (REQ-22).

    The request has no body. The draft's text and the incident are left as they are.
    """
    with transaction() as tx:
        load_session(tx, session_id)
        require_role(role, Operation.ACCEPT_DRAFT)
        # Locked, so of two concurrent acceptances only one finds the draft Proposed.
        draft = load_draft(tx, draft_id, session_id=session_id, for_update=True)
        if draft.status == DraftStatus.ACCEPTED:
            raise TracerError(ErrorCode.CONFLICT, f"Draft {draft_id} is already accepted")

        before = _acceptance(draft)
        draft.status = DraftStatus.ACCEPTED
        draft.accepted_by = user_id
        draft.accepted_at = utcnow()
        audit(
            tx, AuditAction.ACCEPT_DRAFT, draft.incident_id, before, _acceptance(draft),
            user_id=user_id, session_id=session_id,
        )
        return draft_to_dict(draft)


def load_draft(
    tx: Session, draft_id: str, *, session_id: str, for_update: bool = False
) -> Draft:
    """The draft named in the path, or NOT_FOUND; a draft of another session's incident counts
    as unknown. for_update locks only the draft row, which the app role's UPDATE grant on the
    acceptance columns allows."""
    query = (
        select(Draft)
        .join(Incident, Incident.incident_id == Draft.incident_id)
        .where(Draft.draft_id == draft_id, Incident.session_id == session_id)
    )
    if for_update:
        query = query.with_for_update(of=Draft)
    # PostgreSQL text can't hold NUL, so no stored ID has one, and querying for it would fail.
    draft = None if "\x00" in draft_id else tx.scalar(query)
    if draft is None:
        raise TracerError(ErrorCode.NOT_FOUND, f"Draft {draft_id!r} not found")
    return draft


def draft_to_dict(draft: Draft) -> dict[str, Any]:
    """The Draft schema. Every draft is advisory."""
    return {
        "draft_id": draft.draft_id,
        "incident_id": draft.incident_id,
        "form_type": draft.form_type.value,
        "elements": draft.elements,
        "advisory": True,
        "status": draft.status.value,
        "accepted_by": draft.accepted_by,
        "accepted_at": draft.accepted_at.isoformat() if draft.accepted_at else None,
    }


def _acceptance(draft: Draft) -> dict[str, Any]:
    """The fields acceptance changes, for the audit entry's before and after."""
    return {
        "draft_id": draft.draft_id,
        "status": draft.status.value,
        "accepted_by": draft.accepted_by,
        "accepted_at": draft.accepted_at.isoformat() if draft.accepted_at else None,
    }
