"""server.py's mounts, the system's health check and its 501 stub."""

import socket
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api as system_api
import app.db as db
from app.config import get_settings
from app.db import get_engine
from server import app as server_app

client = TestClient(server_app)


def test_health_reaches_the_database():
    response = client.get("/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_an_unreachable_database(monkeypatch):
    def unreachable():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(system_api, "get_engine", unreachable)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_health_without_database_url(monkeypatch, tmp_path):
    # As on a preview deployment: no DATABASE_URL in the environment and no .env file.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        response = client.get("/system/health")
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_gives_up_on_a_silent_database(monkeypatch):
    # A socket that accepts connections but never answers, like a database that stopped responding.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg://tracer_app:x@127.0.0.1:{port}/tracer")
        # libpq treats anything under 2 seconds as 2.
        monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 2)
        get_settings.cache_clear()
        get_engine.cache_clear()
        try:
            started = time.monotonic()
            response = client.get("/system/health")
            elapsed = time.monotonic() - started
        finally:
            get_settings.cache_clear()
            get_engine.cache_clear()
    assert response.status_code == 503
    assert elapsed < 8


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/system/incidents", "/system/incidents/inc_1/audit"])
def test_other_system_paths_return_501(method, path):
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NOT_IMPLEMENTED"
    assert body["message"]
    assert "field" not in body


def test_root_serves_nothing():
    assert client.get("/").status_code == 404
