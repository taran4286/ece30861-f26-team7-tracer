"""The project operations this team builds, each passed to its service."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.calls import call, read_body
from app.services import evidence, filings, incidents, workflow

router = APIRouter()


@router.post("/incidents")
async def create_incident(request: Request) -> JSONResponse:
    return await call(request, incidents.create_incident, await read_body(request), status=201)


@router.get("/incidents/{incident_id}")
async def get_incident(request: Request, incident_id: str) -> JSONResponse:
    return await call(request, incidents.get_incident, incident_id)


@router.post("/incidents/{incident_id}/evidence/import")
async def import_evidence(request: Request, incident_id: str) -> JSONResponse:
    body = await read_body(request)
    return await call(request, evidence.import_evidence, incident_id, body, status=201)


@router.post("/incidents/{incident_id}/determination")
async def record_determination(request: Request, incident_id: str) -> JSONResponse:
    body = await read_body(request)
    return await call(request, workflow.record_determination, incident_id, body)


@router.post("/incidents/{incident_id}/transitions")
async def transition_incident(request: Request, incident_id: str) -> JSONResponse:
    body = await read_body(request)
    return await call(request, workflow.transition_incident, incident_id, body)


@router.get("/incidents/{incident_id}/audit")
async def list_audit_entries(request: Request, incident_id: str) -> JSONResponse:
    return await call(request, workflow.list_audit_entries, incident_id)


@router.post("/incidents/{incident_id}/filings")
async def record_filing(request: Request, incident_id: str) -> JSONResponse:
    """Record an external filing and return the updated incident with status 201."""
    body = await read_body(request)
    return await call(request, filings.record_filing, incident_id, body, status=201)
