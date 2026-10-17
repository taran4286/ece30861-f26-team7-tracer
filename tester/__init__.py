"""Our tester's FastAPI app; server.py mounts it at /tester.

It serves a photo at GET / and returns 501 for every other path. The tester reaches
systems over HTTP only and never imports `app`.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]

PHOTO = Path(__file__).with_name("tester.png")

tester_app = FastAPI(title="Tracer tester")


@tester_app.get("/")
def photo() -> FileResponse:
    # Shows in a browser that the deploy is up; not part of the spec.
    return FileResponse(PHOTO, media_type="image/png")


# Declared last: it matches every path, so a route declared after it never runs.
@tester_app.api_route("/{path:path}", methods=ALL_METHODS)
def not_implemented(path: str) -> JSONResponse:
    # Test-control errors use {message, source}, not the project Error schema.
    return JSONResponse(
        {"message": f"/{path} is not implemented", "source": "tester"}, status_code=501
    )
