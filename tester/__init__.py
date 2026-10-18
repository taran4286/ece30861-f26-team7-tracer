"""Our tester's FastAPI app; server.py mounts it at /tester.

It serves a captioned photo at GET /, the Tester API's setup, run and report routes, and 501
for every other path. runner.py does the work of setup and run; sessions and reports live in
Postgres (see sessions.py). The tester reaches systems over HTTP only and never imports `app`.
"""

import base64
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from tester.runner import checked_target_url, prepare, run_session
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


async def _respond(work: Callable[[], Awaitable[Any]], status: int = 200) -> JSONResponse:
    """Answer with the result of `work`, or with its TestControlError."""
    try:
        return JSONResponse(await work(), status_code=status)
    except ControlError as error:
        return JSONResponse(error.to_dict(), status_code=error.status)
    except (ValidationError, ImportError, SQLAlchemyError):
        # The causes /system/health reports: a missing or invalid setting, such as no
        # TESTER_DATABASE_URL on a preview, a driver we don't install, or no database.
        logger.warning("tester storage unavailable", exc_info=True)
        return JSONResponse(
            {"message": "Tester storage is unavailable", "source": "tester"}, status_code=500
        )
    except Exception as exc:
        logger.exception("tester fault")
        return JSONResponse(
            {"message": f"Tester fault: {type(exc).__name__}", "source": "tester"}, status_code=500
        )


async def _field(request: Request, name: str) -> Any:
    """One field of a JSON object body; 400 when the body isn't a JSON object."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ControlError(400, "Expected a JSON object body")
    return body.get(name)


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ControlError(400, "session_id is required")
    return value


# setup and run block on the target and the database, so they run in the thread pool.


@tester_app.post("/test/setup")
async def setup(request: Request) -> JSONResponse:
    """Create a session and prepare the target system (TEST-03). Never runs the suite."""

    async def work() -> dict[str, str]:
        target_url = checked_target_url(await _field(request, "target_url"))
        return {"session_id": await run_in_threadpool(prepare, target_url)}

    return await _respond(work, status=201)


@tester_app.post("/test/run")
async def run(request: Request) -> JSONResponse:
    """Run the suite once against a prepared session and return its report (TEST-04)."""

    async def work() -> dict[str, Any]:
        session_id = _session_id(await _field(request, "session_id"))
        return await run_in_threadpool(run_session, session_id)

    return await _respond(work)


@tester_app.get("/test/report")
async def report(session_id: str | None = None) -> JSONResponse:
    """Return a finished run's stored SuiteReport (TEST-05). Never starts or repeats a run."""

    async def work() -> dict[str, Any]:
        return await run_in_threadpool(get_report, _session_id(session_id))

    return await _respond(work)


# Declared last: it matches every path, so a route declared after it never runs.
@tester_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    # Test-control errors use {message, source}, not the project Error schema.
    return JSONResponse(
        {"message": f"/{path} is not implemented", "source": "tester"}, status_code=501
    )
