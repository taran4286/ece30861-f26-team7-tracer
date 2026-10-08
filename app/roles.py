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
        raise TracerError(
            ErrorCode.FORBIDDEN_ROLE, f"Role {role!r} is not allowed to {operation.value}"
        )
