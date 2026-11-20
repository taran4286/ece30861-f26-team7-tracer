"""Generate an advisory draft with the session's LLM, validate it, and accept a stored draft."""

import ipaddress
import logging
import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.clock import utcnow
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditAction, Draft, DraftStatus, Evidence, Incident, State
from app.roles import Operation, Role, require_role
from app.services import llm
from app.services.incidents import evidence_to_dict, load_incident
from app.services.validation import invalid, require_object, require_string
from app.services.workflow import DRAFT_ELEMENTS
from app.sessions import load_session


_DRAFT_STATES = frozenset({
    State.DETERMINED_MATERIAL, State.DISCLOSURE_DELAYED, State.AMENDMENT_PENDING,
})
_UNDETERMINED = "Not yet determined"
# An element's text is a short paragraph. Longer text fails validation, which also bounds the
# time scan_sensitive can take.
MAX_TEXT_LENGTH = 10_000

logger = logging.getLogger(__name__)


def request_draft(
    incident_id: str, *, user_id: str, role: Role | str, session_id: str
) -> dict[str, Any]:
    """POST /incidents/{incident_id}/drafts: store a Proposed advisory draft (REQ-15 to REQ-17,
    REQ-20). The request has no body; user_id keeps the common service signature.

    The LLM call sits between two short transactions, so no connection or lock is held while
    it runs. Any provider failure, or output that fails validation, is a 503 with nothing
    written; every client error comes before the call. No audit entry, and the incident's
    state and version are unchanged.

    llm.TIMEOUT_SECONDS is the budget from here to the provider's last byte, so a slow
    database read leaves the call less time rather than pushing the 503 later.
    """
    started = time.monotonic()
    context = load_draft_context(incident_id, role=role, session_id=session_id)
    evidence_ids = {item["evidence_id"] for item in context["evidence"]}
    if evidence_ids:
        remaining = llm.TIMEOUT_SECONDS - (time.monotonic() - started)
        elements = _generate(context, evidence_ids, remaining)
    else:
        elements = {name: _undetermined() for name in DRAFT_ELEMENTS}

    with transaction() as tx:
        incident = load_incident(tx, incident_id, session_id=session_id, for_update=True)
        # The incident may have moved during the call, even to another state that allows
        # drafting; the text was written for its old state and form.
        _require_draftable(incident)
        if (incident.state.value, incident.required_form.value) != (
            context["state"], context["form_type"],
        ):
            raise TracerError(
                ErrorCode.INVALID_TRANSITION,
                "The incident's state or required form changed while the draft was generated; "
                "request a new draft",
            )
        draft = Draft(
            incident_id=incident_id, form_type=incident.required_form, elements=elements,
            status=DraftStatus.PROPOSED,
        )
        tx.add(draft)
        tx.flush()
        return draft_to_dict(draft)


def _generate(
    context: dict[str, Any], evidence_ids: set[str], timeout: float,
) -> dict[str, Any]:
    """The provider's elements, validated, with sensitive flags checked and added (REQ-17).
    timeout is what's left of the request's budget for the call."""
    try:
        elements = llm.draft_elements(context, timeout=timeout)
        validate_draft_elements(elements, evidence_ids=evidence_ids)
    except (llm.ProviderError, TracerError) as error:
        # The cause goes to the log, never the response.
        logger.warning("drafting unavailable for %s: %r", context["incident_id"], error)
        raise TracerError(ErrorCode.DRAFTING_UNAVAILABLE, "Drafting unavailable") from None
    return {
        name: {
            "text": elements[name]["text"],
            "citations": list(elements[name]["citations"]),
            "sensitive_flags": scan_sensitive(
                elements[name]["text"], elements[name]["sensitive_flags"]
            ),
        }
        for name in DRAFT_ELEMENTS
    }


def _undetermined() -> dict[str, Any]:
    return {"text": _UNDETERMINED, "citations": [], "sensitive_flags": []}


# Deterministic REQ-17 patterns, each with its SensitiveFlag reason.
_SENSITIVE = [
    (re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE), "CVE"),
    (re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.]*\d)"), "IP address"),
    # At most 126 labels before the last one, more than a DNS name's 253 characters can hold.
    # Unbounded, a long dotted run that never ends in a valid last label was rescanned from
    # every label, so the time grew with the square of its length.
    (re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,126}[a-z]{2,63}\b",
                re.IGNORECASE), "hostname"),
    (re.compile(r"\bunpatched\b[\w -]{0,40}?\b(?:system|server|host|device|software)s?\b|"
                r"\bunpatched\b", re.IGNORECASE), "unpatched system"),
    (re.compile(r"\bfirewall rules?\b", re.IGNORECASE), "firewall rule"),
]
# A "hostname" ending in one of these is a file name, as evidence often names files (ransomware
# appends .locked or .encrypted). A few, such as zip and mov, are also real top-level domains;
# a file name is the likelier reading in incident text.
_FILE_EXTENSIONS = frozenset({
    "bak", "bat", "cfg", "conf", "crt", "csv", "dat", "db", "dll", "doc", "docx", "eml",
    "encrypted", "exe", "gif", "gz", "htm", "html", "ini", "iso", "jar", "jpeg", "jpg", "js",
    "json", "key", "locked", "log", "mov", "mp4", "msg", "msi", "pdf", "pem", "png", "ppt",
    "pptx", "py", "rar", "sh", "sql", "tar", "tmp", "txt", "vbs", "xls", "xlsx", "xml", "yaml",
    "yml", "zip",
})
# IPv6 candidates: hex groups with at least two colons, confirmed by ipaddress.
_IPV6_CANDIDATE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")


def scan_sensitive(text: str, model_flags: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Sensitive flags for one element's text (REQ-17): the model's flags whose excerpt really
    appears in the text, then every pattern match not already flagged, in text order.

    The placeholder "Not yet determined" has none."""
    if text == _UNDETERMINED:
        return []
    flags: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(excerpt: str, reason: str) -> None:
        if excerpt and excerpt in text and excerpt not in seen:
            seen.add(excerpt)
            flags.append({"excerpt": excerpt, "reason": reason})

    for flag in model_flags:
        add(flag["excerpt"], flag["reason"])
    found: list[tuple[int, str, str]] = []
    for pattern, reason in _SENSITIVE:
        found += [
            (match.start(), match.group(), reason) for match in pattern.finditer(text)
            if not (reason == "hostname" and _is_file_name(match.group()))
        ]
    for match in _IPV6_CANDIDATE.finditer(text):
        try:
            ipaddress.IPv6Address(match.group())
        except ValueError:
            continue
        found.append((match.start(), match.group(), "IP address"))
    for _, excerpt, reason in sorted(found):
        add(excerpt, reason)
    return flags


def _is_file_name(match: str) -> bool:
    return match.rsplit(".", 1)[-1].lower() in _FILE_EXTENSIONS


def load_draft_context(
    incident_id: str, *, role: Role | str, session_id: str,
) -> dict[str, Any]:
    """Read the eligible incident and its evidence before calling a draft provider."""
    with transaction() as tx:
        session = load_session(tx, session_id)
        require_role(role, Operation.REQUEST_DRAFT)
        incident = load_incident(tx, incident_id, session_id=session_id)
        _require_draftable(incident)
        evidence = tx.scalars(
            select(Evidence)
            .where(Evidence.incident_id == incident_id)
            .order_by(Evidence.imported_at, Evidence.evidence_id)
        )
        return {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "discovery_date": incident.discovery_date.isoformat(),
            "state": incident.state.value,
            "form_type": incident.required_form.value,
            "llm_base_url": session.llm_base_url,
            "evidence": [evidence_to_dict(item) for item in evidence],
        }


def _require_draftable(incident: Incident) -> None:
    """409 unless the state allows drafting: NOT_MATERIAL before a Material determination,
    INVALID_TRANSITION for any other ineligible state."""
    if incident.state in {State.UNDER_ASSESSMENT, State.DETERMINED_NOT_MATERIAL}:
        raise TracerError(ErrorCode.NOT_MATERIAL, "A draft requires a material incident")
    if incident.state not in _DRAFT_STATES:
        raise TracerError(ErrorCode.INVALID_TRANSITION, "The incident state does not allow drafting")


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
        if len(text) > MAX_TEXT_LENGTH:
            raise invalid(f"{field}.text", f"Expected at most {MAX_TEXT_LENGTH} characters")
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
