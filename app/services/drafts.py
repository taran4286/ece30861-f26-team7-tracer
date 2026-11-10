"""Read the inputs for a draft and validate its evidence citations."""

from typing import Any

from sqlalchemy import select

from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import Evidence, FormType, State
from app.roles import Operation, Role, require_role
from app.services.incidents import evidence_to_dict, load_incident
from app.services.validation import invalid, require_object, require_string
from app.services.workflow import DRAFT_ELEMENTS
from app.sessions import load_session


_DRAFT_STATES = frozenset({
    State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED, State.AMENDMENT_PENDING,
})
_DRAFT_FORMS = frozenset({FormType.FORM_8K_ITEM_105, FormType.FORM_6K, FormType.FORM_8KA})
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
        if incident.required_form not in _DRAFT_FORMS:
            raise TracerError(ErrorCode.CONFLICT, "The incident has no supported draft form")
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
