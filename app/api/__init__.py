"""The system's FastAPI app; server.py mounts it at /system.

It serves a status page, a health check, the test-control routes, the project operations
this team builds, and 405 or 501 for the rest of the spec's operations. Other paths get 501.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api import pages
from app.api.calls import error_response
from app.api.control import router as control_router
from app.api.routes import router as project_router
from app.api.stubs import router as stub_router
from app.db import get_engine
from app.errors import ErrorCode, TracerError

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

system_app = FastAPI(title="Tracer system")


def database_ok() -> bool:
    """Whether the function reaches the database as the app role."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except (ValidationError, ImportError, SQLAlchemyError):
        # ValidationError: a setting is missing or invalid, e.g. no DATABASE_URL on a preview.
        # ImportError: the URL names a driver we don't install, e.g. a bare postgresql:// scheme.
        # SQLAlchemyError includes a malformed URL; get_engine() raises ArgumentError for it.
        logger.warning("database unavailable", exc_info=True)
        return False
    return True


@system_app.get("/")
def status_page() -> HTMLResponse:
    """The status page: health, the spec, and how each operation is answered. Not in the spec."""
    return HTMLResponse(pages.render(database_ok()))


@system_app.get("/health")
def health() -> JSONResponse:
    """Check that the function reaches the database as the app role. Not part of the spec."""
    if not database_ok():
        return JSONResponse({"status": "unavailable", "database": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "ok"})


@system_app.exception_handler(TracerError)
async def tracer_error(request: Request, error: TracerError) -> JSONResponse:
    """A project operation's error, as the Error schema with the code's status."""
    return error_response(error)


# Every route must be registered before the catch-all.
system_app.include_router(control_router)
system_app.include_router(project_router)
system_app.include_router(stub_router)


# Declared last: it matches every path, so a route declared after it never runs.
@system_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    # The Error schema types `field` as a string, so it's left out rather than sent as null.
    return JSONResponse(
        {"error": ErrorCode.NOT_IMPLEMENTED.value, "message": f"/{path} is not implemented"},
        status_code=501,
    )
