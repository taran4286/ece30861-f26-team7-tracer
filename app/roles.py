"""Roles and the one permission map every operation checks against."""

from enum import StrEnum

from app.errors import ErrorCode, TracerError


class Role(StrEnum):
    RESPONDER = "Responder"
    DISCLOSURE_APPROVER = "DisclosureApprover"
    BOARD = "Board"


class Operation(StrEnum):
    READ = "read"  # every GET
    CREATE_INCIDENT = "create_incident"
    IMPORT_EVIDENCE = "import_evidence"
    TAG_EVIDENCE = "tag_evidence"
    CREATE_GROUP = "create_group"
    RECORD_DETERMINATION = "record_determination"
    TRANSITION = "transition"
    RECORD_FILING = "record_filing"
    REQUEST_DRAFT = "request_draft"
    ACCEPT_DRAFT = "accept_draft"
    # PUT, PATCH or DELETE on a record that is never replaced or deleted (REQ-22). It always
    # ends in 405; only a role allowed every operation gets that far.
    REPLACE_OR_DELETE = "replace_or_delete"


PERMISSIONS: dict[Role, frozenset[Operation]] = {
    Role.RESPONDER: frozenset(
        {
            Operation.READ,
            Operation.CREATE_INCIDENT,
            Operation.IMPORT_EVIDENCE,
            Operation.TAG_EVIDENCE,
            Operation.CREATE_GROUP,
        }
    ),
    Role.DISCLOSURE_APPROVER: frozenset(Operation),
    Role.BOARD: frozenset({Operation.READ}),
}


def require_role(role: Role | str, operation: Operation) -> None:
    """Raise FORBIDDEN_ROLE unless `role` may perform `operation`. Call before any write."""
    try:
        allowed = PERMISSIONS[Role(role)]
    except ValueError:
        allowed = frozenset()
    if operation not in allowed:
        # str() gives a Role's value ("Board"), not its repr.
        raise TracerError(
            ErrorCode.FORBIDDEN_ROLE, f"Role {str(role)!r} is not allowed to {operation.value}"
        )
