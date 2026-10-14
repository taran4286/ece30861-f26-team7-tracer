"""server.py's mounts, the system's health check and its 501 stub."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api as system_api
from app.config import get_settings
from app.db import get_engine
from server import app as server_app

client = TestClient(server_app)


def test_health_reaches_the_database():
    """Verify the mounted health route returns 200 when the local database is reachable."""
    response = client.get("/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_an_unreachable_database(monkeypatch):
    """Verify a database connection failure produces a 503 unavailable response."""
    def unreachable():
        """Simulate an engine lookup that fails with a database connection error."""
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(system_api, "get_engine", unreachable)
    response = client.get("/system/health")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_health_without_database_url(monkeypatch, tmp_path):
    """Verify the health route returns 503 when no database URL is configured."""
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


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/system/incidents", "/system/incidents/inc_1/audit"])
def test_other_system_paths_return_501(method, path):
    """Verify system stubs return 501 with an error message and no field key."""
    response = client.request(method, path)
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "NOT_IMPLEMENTED"
    assert body["message"]
    assert "field" not in body


def test_root_serves_nothing():
    """Verify the deployment's root path returns 404."""
    assert client.get("/").status_code == 404
