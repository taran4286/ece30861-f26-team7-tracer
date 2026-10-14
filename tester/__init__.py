"""Our tester's FastAPI app; server.py mounts it at /tester.

It returns 501 for every path. The tester reaches systems over HTTP only and never
imports `app`.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

tester_app = FastAPI(title="Tracer tester")


@tester_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    """Return a 501 response in the test-control error format for an unimplemented path."""
    # Test-control errors use {message, source}, not the project Error schema.
    return JSONResponse(
        {"message": f"/{path} is not implemented", "source": "tester"}, status_code=501
    )
