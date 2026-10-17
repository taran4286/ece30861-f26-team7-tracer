"""Our tester's FastAPI app; server.py mounts it at /tester.

It serves a captioned photo at GET /, stored suite reports at GET /test/report, and 501 for
every other path. Sessions and reports live in Postgres (see sessions.py). The tester
reaches systems over HTTP only and never imports `app`.
"""

import base64
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from tester.sessions import ControlError, get_report

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

PHOTO = Path(__file__).with_name("tester.png")
CAPTION = "Team 7 tester"
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

tester_app = FastAPI(title="Tracer tester")


@tester_app.get("/")
def photo() -> HTMLResponse:
    # Shows in a browser that the deploy is up; not part of the spec. Read per request, not at
    # import: a missing file then breaks only this page.
    encoded = base64.b64encode(PHOTO.read_bytes()).decode()
    return HTMLResponse(PAGE.format(caption=CAPTION, photo=encoded))


@tester_app.get("/test/report")
def report(session_id: str | None = None) -> JSONResponse:
    """Return a finished run's stored SuiteReport (TEST-05). Never starts or repeats a run."""
    try:
        if session_id is None or not session_id.strip():
            raise ControlError(400, "session_id is required")
        return JSONResponse(get_report(session_id))
    except ControlError as error:
        return JSONResponse(error.to_dict(), status_code=error.status)
    except (ValidationError, ImportError, SQLAlchemyError):
        # The causes /system/health reports: a missing or invalid setting, such as no
        # TESTER_DATABASE_URL on a preview, a driver we don't install, or no database.
        logger.warning("report: tester storage unavailable", exc_info=True)
        return JSONResponse(
            {"message": "Report storage is unavailable", "source": "tester"}, status_code=500
        )


# Declared last: it matches every path, so a route declared after it never runs.
@tester_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    # Test-control errors use {message, source}, not the project Error schema.
    return JSONResponse(
        {"message": f"/{path} is not implemented", "source": "tester"}, status_code=501
    )
