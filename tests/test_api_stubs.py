"""Stub errors follow header/role order and never reach the database."""

import pytest
from fastapi.testclient import TestClient

from app import db
from server import app

client = TestClient(app)
HEADERS = {"X-User-Id": "stub-reader", "X-User-Role": "Responder", "X-Test-Run": "stub-run"}
ROUTES = [("PUT", "/system/incidents/example/audit", 405),
          ("PATCH", "/system/incidents/example/audit", 405),
          ("DELETE", "/system/incidents/example/audit", 405),
          ("PATCH", "/system/incidents/example/evidence/item", 501),
          ("POST", "/system/groups", 501)]


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Fail the test if a stub tries to access the database."""
    def forbidden():
        """Report unexpected database access."""
        pytest.fail("A placeholder must not access the database")
    monkeypatch.setattr(db, "get_engine", forbidden)
    monkeypatch.setattr(db, "_session_factory", forbidden)


@pytest.mark.parametrize("method,path,status", ROUTES)
@pytest.mark.parametrize("role", ["Responder", "DisclosureApprover", "Board"])
def test_role_and_response(method, path, status, role):
    """Check each role receives the expected 403, 405 or 501 response."""
    response = client.request(method, path, headers={**HEADERS, "X-User-Role": role})
    forbidden = role == "Board" and status == 501
    assert response.status_code == (403 if forbidden else status)
    expected = "FORBIDDEN_ROLE" if forbidden else ("METHOD_NOT_ALLOWED" if status == 405 else "NOT_IMPLEMENTED")
    assert response.json()["error"] == expected
    assert response.json()["message"]
    assert "field" not in response.json()


@pytest.mark.parametrize("method,path,status", ROUTES)
@pytest.mark.parametrize("field,value", [("X-User-Id", None), ("X-User-Id", " "),
    ("X-User-Role", None), ("X-User-Role", "unknown"), ("X-Test-Run", None), ("X-Test-Run", "")])
def test_invalid_headers_before_role(method, path, status, field, value):
    """Check invalid headers are rejected before role permissions."""
    headers = {**HEADERS, "X-User-Role": "Board"}
    if value is None:
        headers.pop(field)
    else:
        headers[field] = value
    response = client.request(method, path, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"] == "VALIDATION_ERROR"
    assert response.json()["field"] == field


def test_first_invalid_header():
    """Check missing headers report X-User-Id first."""
    response = client.post("/system/groups")
    assert response.status_code == 400
    assert response.json()["field"] == "X-User-Id"
