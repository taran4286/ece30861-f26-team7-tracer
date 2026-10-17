"""The system's FastAPI app; server.py mounts it at /system.

It serves a photo at GET /, GET /health, and 501 for every other path.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_engine
from app.errors import ErrorCode

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

PHOTO = Path(__file__).with_name("system.png")

system_app = FastAPI(title="Tracer system")


@system_app.get("/")
def photo() -> FileResponse:
    """Show a photo at the base URL, so a browser shows the deploy is up. Not part of the spec."""
    return FileResponse(PHOTO, media_type="image/png")


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


# Declared last: it matches every path, so a route declared after it never runs.
@system_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    # The Error schema types `field` as a string, so it's left out rather than sent as null.
    return JSONResponse(
        {"error": ErrorCode.NOT_IMPLEMENTED.value, "message": f"/{path} is not implemented"},
        status_code=501,
    )
