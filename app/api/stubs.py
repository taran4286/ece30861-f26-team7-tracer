"""Explicit Phase 3 placeholders and protection for the append-only audit trail."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.errors import ErrorCode, TracerError
from app.roles import Operation, Role, require_role

router = APIRouter()


def _check_headers(request: Request) -> Role:
    """Check raw headers here so missing headers return the contract's 400, not 422."""
    user_id = request.headers.get("X-User-Id")
    if not user_id or not user_id.strip():
        raise TracerError(ErrorCode.VALIDATION_ERROR, "User ID is required", field="X-User-Id")
    try:
        role = Role(request.headers.get("X-User-Role"))
    except (ValueError, TypeError):
        raise TracerError(ErrorCode.VALIDATION_ERROR, "Invalid user role", field="X-User-Role")
    run = request.headers.get("X-Test-Run")
    if not run or not run.strip():
        raise TracerError(ErrorCode.VALIDATION_ERROR, "Test run is required", field="X-Test-Run")
    return role


def _reply(request: Request, operation: Operation, code: ErrorCode, status: int) -> JSONResponse:
    """Headers and permissions come before the placeholder response; nothing is written."""
    try:
        require_role(_check_headers(request), operation)
    except TracerError as error:
        body = {key: value for key, value in error.to_dict().items() if value is not None}
        return JSONResponse(body, status_code=400 if error.code == ErrorCode.VALIDATION_ERROR else 403)
    return JSONResponse({"error": code.value, "message": (
        "Audit entries cannot be edited or deleted" if status == 405
        else "This feature is not implemented in Phase 3"
    )}, status_code=status)


@router.api_route("/incidents/{incident_id}/audit", methods=["PUT", "PATCH", "DELETE"])
def audit_not_allowed(request: Request, incident_id: str) -> JSONResponse:
    """Reject attempts to replace, edit or delete audit history."""
    # All recognized roles receive 405. READ uses the existing all-role permission.
    return _reply(request, Operation.READ, ErrorCode.METHOD_NOT_ALLOWED, 405)


@router.patch("/incidents/{incident_id}/evidence/{evidence_id}")
def tag_evidence_stub(request: Request, incident_id: str, evidence_id: str) -> JSONResponse:
    """Return the placeholder response for evidence tagging after permission checks."""
    return _reply(request, Operation.TAG_EVIDENCE, ErrorCode.NOT_IMPLEMENTED, 501)


@router.post("/groups")
def create_group_stub(request: Request) -> JSONResponse:
    """Return the placeholder response for group creation after permission checks."""
    return _reply(request, Operation.CREATE_GROUP, ErrorCode.NOT_IMPLEMENTED, 501)
