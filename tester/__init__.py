"""Our tester's FastAPI app; server.py mounts it at /tester.

It serves a page at GET / with how to run it and any stored report, the Tester API's setup,
run and report routes, and 501 for every other path. runner.py does the work of setup and run;
sessions and reports live in Postgres (see sessions.py). The tester reaches systems over HTTP
only and never imports `app`.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from tester import pages
from tester.runner import checked_target_url, prepare, run_session
from tester.sessions import ControlError, get_report

logger = logging.getLogger(__name__)

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

tester_app = FastAPI(title="Tracer tester")


@tester_app.get("/")
def page(session_id: str | None = None) -> HTMLResponse:
    """How to run the tester, and the stored report for `session_id` when one is asked for.

    Not part of the spec. Storage is read only when a session ID is given, so the page also
    loads on previews, which have no TESTER_DATABASE_URL.
    """
    report = problem = None
    if session_id and session_id.strip():
        try:
            if "\x00" in session_id:
                # PostgreSQL text can't hold NUL, so no stored session has one.
                raise ControlError(404, f"Unknown session {session_id!r}")
            report = get_report(session_id)
        except ControlError as error:
            problem = error.message
        except (ValidationError, ImportError, SQLAlchemyError):
            logger.warning("tester storage unavailable", exc_info=True)
            problem = "Tester storage is unavailable"
    return HTMLResponse(pages.render(session_id, report, problem))


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
