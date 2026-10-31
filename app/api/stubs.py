"""Routes that end in 405 or 501 (root "Stubs"), after the same checks as a real operation.

Each checks the identity headers, then its placeholder service checks the session, the role
and the IDs in the path, so the 405 or 501 comes last in the error order.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.calls import call, error_response
from app.errors import ErrorCode, TracerError
from app.roles import Operation
from app.services import placeholders

router = APIRouter()


async def _refuse(request: Request, allow: str, *ids: str) -> JSONResponse:
    """405 METHOD_NOT_ALLOWED, with an Allow header naming the methods the path does have."""
    try:
        return await call(request, placeholders.refuse_change, *ids)
    except TracerError as error:
        if error.code != ErrorCode.METHOD_NOT_ALLOWED:
            raise
        return error_response(error, headers={"Allow": allow})


@router.api_route("/incidents/{incident_id}", methods=["PUT", "DELETE"])
async def incident_not_allowed(request: Request, incident_id: str) -> JSONResponse:
    """Incidents are never replaced or deleted (REQ-22)."""
    return await _refuse(request, "GET", incident_id)


@router.delete("/incidents/{incident_id}/evidence/{evidence_id}")
async def evidence_not_allowed(request: Request, incident_id: str, evidence_id: str) -> JSONResponse:
    """Evidence is never deleted (REQ-22)."""
    return await _refuse(request, "PATCH", incident_id, evidence_id)


@router.api_route("/incidents/{incident_id}/audit", methods=["PUT", "PATCH", "DELETE"])
async def audit_not_allowed(request: Request, incident_id: str) -> JSONResponse:
    """The audit trail is append-only (REQ-22)."""
    return await _refuse(request, "GET", incident_id)


@router.patch("/incidents/{incident_id}/evidence/{evidence_id}")
async def tag_evidence(request: Request, incident_id: str, evidence_id: str) -> JSONResponse:
    """Tagging evidence (REQ-05) isn't built."""
    return await call(
        request, placeholders.not_built, Operation.TAG_EVIDENCE, incident_id, evidence_id
    )


@router.post("/groups")
async def create_group(request: Request) -> JSONResponse:
    """Incident groups (REQ-04) aren't built."""
    return await call(request, placeholders.not_built, Operation.CREATE_GROUP)


@router.post("/incidents/{incident_id}/drafts")
async def request_draft(request: Request, incident_id: str) -> JSONResponse:
    """Drafts aren't built."""
    return await call(request, placeholders.not_built, Operation.REQUEST_DRAFT, incident_id)


@router.post("/drafts/{draft_id}/accept")
async def accept_draft(request: Request, draft_id: str) -> JSONResponse:
    """Drafts aren't built, so no draft exists to look up."""
    return await call(request, placeholders.not_built, Operation.ACCEPT_DRAFT)
