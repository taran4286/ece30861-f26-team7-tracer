"""The test-control routes (TEST-01, TEST-02, TEST-06).

They take no identity headers, and their errors use the spec's TestControlError
({message, source}), not the project Error schema.
"""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.api.calls import read_body
from app.errors import TracerError
from app.services import control

logger = logging.getLogger(__name__)

router = APIRouter()


async def _respond(work: Callable[..., Any], *args: Any, status: int = 200) -> JSONResponse:
    """Run the blocking service in the thread pool; answer with its result or a TestControlError."""
    try:
        return JSONResponse(await run_in_threadpool(work, *args), status_code=status)
    except TracerError as error:
        return JSONResponse({"message": error.message, "source": "system"}, status_code=error.status)
    except (ValidationError, ImportError, SQLAlchemyError):
        # The causes /health reports: a missing or invalid setting, such as no DATABASE_URL on a
        # preview, a driver we don't install, a malformed URL, or no database. The spec keeps
        # setup reachable then.
        logger.warning("system storage unavailable", exc_info=True)
        return JSONResponse(
            {"message": "System storage is unavailable", "source": "system"}, status_code=500
        )
    except Exception as exc:
        logger.exception("system fault")
        return JSONResponse(
            {"message": f"System fault: {type(exc).__name__}", "source": "system"}, status_code=500
        )


@router.post("/test/setup")
async def setup(request: Request) -> JSONResponse:
    """Prepare a new, empty session with the supplied ID (TEST-01). Runs no tests."""
    return await _respond(control.setup_session, await read_body(request), status=201)


@router.get("/test/report")
async def coverage(session_id: str | None = None) -> JSONResponse:
    """A session's code coverage (TEST-02). Runs no project operations."""
    return await _respond(control.coverage_report, session_id)


@router.get("/test/report/{run_id}")
async def snapshot(run_id: str) -> JSONResponse:
    """The session's settings, incidents and audit trail (TEST-06)."""
    return await _respond(control.snapshot, run_id)


@router.patch("/test/sessions/{session_id}/settings")
def update_settings(session_id: str) -> JSONResponse:
    """The optional settings extension (TEST-07) isn't built."""
    return JSONResponse(
        {"message": "/test/sessions/{session_id}/settings is not implemented", "source": "system"},
        status_code=501,
    )
