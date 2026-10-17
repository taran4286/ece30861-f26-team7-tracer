"""server.py's mounts, the system's health check and its 501 stub."""

import base64
import socket
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api as system_api
import app.db as db
from app.config import get_settings
from server import app as server_app

client = TestClient(server_app)


def _clear_cached_settings():
    get_settings.cache_clear()
    db.get_engine.cache_clear()
    db._session_factory.cache_clear()


@pytest.fixture
def fresh_settings():
    # Settings, the engine and the session factory are cached (see app/AGENTS.md); clear them
    # around a test that changes the environment, so later tests don't inherit its database.
    _clear_cached_settings()
    yield
    _clear_cached_settings()


def test_health_reaches_the_database():
    response = client.get("/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_a_database_error(monkeypatch):
    def failing_engine():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(system_api, "get_engine", failing_engine)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_health_without_database_url(monkeypatch, tmp_path, fresh_settings):
    # As on a preview deployment: no DATABASE_URL in the environment and no .env file.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_with_a_bare_postgresql_url(monkeypatch, fresh_settings):
    # Neon's connection string starts with postgresql://, which makes SQLAlchemy look for
    # psycopg2 instead of psycopg; the deployment guide says to change the scheme.
    monkeypatch.setenv("DATABASE_URL", "postgresql://tracer_app:x@127.0.0.1:5432/tracer")
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_gives_up_on_a_silent_database(monkeypatch, fresh_settings):
    # A socket that accepts connections but never answers, like a database that stopped responding.
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        port = silent.getsockname()[1]
        monkeypatch.setenv("DATABASE_URL", f"postgresql+psycopg://tracer_app:x@127.0.0.1:{port}/tracer")
        # psycopg treats anything under 2 seconds as 2.
        monkeypatch.setattr(db, "CONNECT_TIMEOUT_SECONDS", 2)
        started = time.monotonic()
        response = client.get("/system/health")
        elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert elapsed < 8


@pytest.mark.parametrize("path", ["/system", "/system/"])
def test_base_url_shows_the_captioned_system_photo(path):
    # /system redirects to /system/; TestClient follows it.
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<p style=\"margin:0;font-size:1.25rem\">Team 7 service</p>" in response.text
    assert base64.b64encode(system_api.PHOTO.read_bytes()).decode() in response.text


@pytest.mark.parametrize("method,path", [
    (method, "/system/incidents") for method in ["GET", "POST", "PUT", "PATCH", "DELETE"]
] + [(method, "/system/incidents/inc_1/audit") for method in ["GET", "POST"]])
def test_other_system_paths_return_501(method, path):
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NOT_IMPLEMENTED"
    assert body["message"]
    assert "field" not in body


def test_root_serves_nothing():
    assert client.get("/").status_code == 404
