"""The system's FastAPI app; server.py mounts it at /system.

It serves a captioned photo, a health check, the test-control routes, the project operations
this team builds, and 405 or 501 for the rest of the spec's operations. Other paths get 501.
"""

import base64
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.calls import error_response
from app.api.control import router as control_router
from app.api.routes import router as project_router
from app.api.stubs import router as stub_router
from app.db import get_engine
from app.errors import ErrorCode, TracerError

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

PHOTO = Path(__file__).with_name("system.png")
CAPTION = "Team 7 service"
# The photo is inlined, so the page needs no second route.
PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{caption}</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-content:center;justify-items:center;gap:12px;font-family:sans-serif">
<img src="data:image/png;base64,{photo}" alt="{caption}">
<p style="margin:0;font-size:1.25rem">{caption}</p>
</body>
</html>
"""

system_app = FastAPI(title="Tracer system")


@system_app.get("/")
def photo() -> HTMLResponse:
    """Show a captioned photo, so a browser shows the deploy is up. Not part of the spec."""
    # Read per request, not at import: a missing file then breaks only this page.
    encoded = base64.b64encode(PHOTO.read_bytes()).decode()
    return HTMLResponse(PAGE.format(caption=CAPTION, photo=encoded))


@system_app.get("/health")
def health() -> JSONResponse:
    """Check that the function reaches the database as the app role. Not part of the spec."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except (ValidationError, ImportError, SQLAlchemyError):
        # ValidationError: a setting is missing or invalid, e.g. no DATABASE_URL on a preview.
        # ImportError: the URL names a driver we don't install, e.g. a bare postgresql:// scheme.
        logger.warning("health: database unavailable", exc_info=True)
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
