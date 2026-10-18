"""How a project route calls its service: identity headers, JSON body, thread pool, Error body."""

from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.errors import ErrorCode, TracerError
from app.roles import Role


def identity(request: Request) -> dict[str, Any]:
    """The service's keyword arguments, from X-User-Id, X-User-Role and X-Test-Run.

    Checked in that order; a missing or invalid header is a 400 naming it (root "Identity
    headers"). Whether the session exists is the service's first check.
    """
    user_id = request.headers.get("X-User-Id")
    if not user_id or not user_id.strip():
        raise _bad_header("X-User-Id", "X-User-Id is required")
    try:
        role = Role(request.headers.get("X-User-Role"))
    except ValueError:
        roles = ", ".join(role.value for role in Role)
        raise _bad_header("X-User-Role", f"X-User-Role must be one of: {roles}") from None
    session_id = request.headers.get("X-Test-Run")
    if not session_id or not session_id.strip():
        raise _bad_header("X-Test-Run", "X-Test-Run is required")
    return {"user_id": user_id, "role": role, "session_id": session_id}


def _bad_header(name: str, message: str) -> TracerError:
    return TracerError(ErrorCode.VALIDATION_ERROR, message, field=name)


async def read_body(request: Request) -> Any:
    """The parsed JSON body, or None when there is none or it isn't JSON.

    Services reject a body that isn't an object at their body step, so a bad body is a 400
    only after the session, role and incident checks.
    """
    try:
        return await request.json()
    except ValueError:
        return None


async def call(
    request: Request, service: Callable[..., Any], *args: Any, status: int = 200
) -> JSONResponse:
    """Check the identity headers, then run the blocking service in the thread pool.

    A TracerError from either reaches the app's handler, which answers with error_response().
    """
    who = identity(request)
    return JSONResponse(await run_in_threadpool(service, *args, **who), status_code=status)


def error_response(error: TracerError, headers: dict[str, str] | None = None) -> JSONResponse:
    """The Error schema with the code's status. `field` is left out when unset, since the
    schema types it as a string, not null."""
    body = {key: value for key, value in error.to_dict().items() if value is not None}
    return JSONResponse(body, status_code=error.status, headers=headers)
