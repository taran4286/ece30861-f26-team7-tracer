"""The system's FastAPI app; server.py mounts it at /system.

It serves GET /health and returns 501 for every other path.
"""

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db import get_engine
from app.errors import ErrorCode

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

system_app = FastAPI(title="Tracer system")


@system_app.get("/health")
def health() -> JSONResponse:
    """Check that the function reaches the database as the app role. Not part of the spec."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except (ValidationError, SQLAlchemyError):
        # ValidationError: DATABASE_URL is unset, as on preview deployments.
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
