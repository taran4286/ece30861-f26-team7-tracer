"""Operations that end in 405 or 501, after the same checks as a real operation (root "Stubs").

Incidents, evidence and audit entries are never replaced, edited or deleted (REQ-22), and the
operations this team doesn't build return 501. Each first checks the session, the role and the
path IDs in the root error order, so a 405 or 501 never hides a 403 or 404.
"""

from typing import NoReturn

from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.roles import Operation, Role, require_role
from app.services.evidence import load_evidence
from app.services.incidents import load_incident
from app.sessions import load_session


def refuse_change(
    incident_id: str,
    evidence_id: str | None = None,
    *,
    user_id: str,
    role: Role | str,
    session_id: str,
) -> NoReturn:
    """PUT or DELETE on an incident, DELETE on evidence, PUT, PATCH or DELETE on the audit trail.

    user_id keeps the common service signature; nothing is written.
    """
    _check(Operation.REPLACE_OR_DELETE, incident_id, evidence_id, role=role, session_id=session_id)
    raise TracerError(
        ErrorCode.METHOD_NOT_ALLOWED,
        "Incidents, evidence and audit entries are never replaced, edited or deleted",
    )


def not_built(
    operation: Operation,
    incident_id: str | None = None,
    evidence_id: str | None = None,
    *,
    user_id: str,
    role: Role | str,
    session_id: str,
) -> NoReturn:
    """An operation this team doesn't build: tagging, groups, filings and drafts."""
    _check(operation, incident_id, evidence_id, role=role, session_id=session_id)
    raise TracerError(ErrorCode.NOT_IMPLEMENTED, f"{operation.value} is not implemented")


def _check(
    operation: Operation,
    incident_id: str | None,
    evidence_id: str | None,
    *,
    role: Role | str,
    session_id: str,
) -> None:
    """Steps 1 to 3 of every operation: the session, the role, then the IDs in the path."""
    with transaction() as tx:
        load_session(tx, session_id)
        require_role(role, operation)
        if incident_id is not None:
            load_incident(tx, incident_id, session_id=session_id)
        if evidence_id is not None:
            load_evidence(tx, incident_id, evidence_id)
