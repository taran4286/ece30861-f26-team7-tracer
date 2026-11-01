"""The system's dashboard over HTTP (app/api/dashboard.py) and the status page's link to it."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import dashboard
from server import app as server_app
from tests.helpers import change_session

client = TestClient(server_app)


def new_session(**setup: Any) -> str:
    session_id = f"pytest-{uuid.uuid4().hex}"
    response = client.post("/system/test/setup", json={"session_id": session_id, "setup": setup})
    assert response.status_code == 201
    return session_id


def headers(session_id: str, role: str = "Responder") -> dict[str, str]:
    return {"X-User-Id": f"u-{role}", "X-User-Role": role, "X-Test-Run": session_id}


def new_incident(session_id: str, title: str = "Ransomware", discovery: str = "2026-10-05") -> str:
    created = client.post("/system/incidents", headers=headers(session_id),
                          json={"title": title, "discovery_date": discovery})
    assert created.status_code == 201
    return created.json()["incident_id"]


def determine(session_id: str, incident_id: str) -> dict[str, Any]:
    """A Material determination dated 2026-10-05, the session's default today."""
    item = {"ticket_id": "T-1", "summary": "Files locked", "created_at": "2026-10-05T09:00:00Z"}
    imported = client.post(f"/system/incidents/{incident_id}/evidence/import",
                           headers=headers(session_id), json={"source": "ticketing", "items": [item]})
    determined = client.post(
        f"/system/incidents/{incident_id}/determination",
        headers=headers(session_id, "DisclosureApprover"),
        json={"decision": "Material", "determination_date": "2026-10-05", "rationale": "Assessed",
              "evidence_refs": [imported.json()["evidence"][0]["evidence_id"]], "version": 1},
    )
    assert determined.status_code == 200
    return determined.json()


def page(session_id: str) -> str:
    response = client.get("/system/dashboard", params={"session_id": session_id})
    assert response.status_code == 200
    return response.text


def notice_on(session_id: str, day: str) -> str | None:
    """Move the session's clock in storage and read the notice the dashboard then shows."""
    change_session(session_id, test_now=datetime.fromisoformat(f"{day}T15:00:00").replace(tzinfo=UTC))
    text = page(session_id)
    assert f'value="{day}"' in text
    found = [notice for notice, _ in dashboard.NOTICES.values() if notice in text]
    assert len(found) <= 1
    return found[0] if found else None


def test_ac12_notices_and_a_filing_clearing_them():
    session_id = new_session()
    incident_id = new_incident(session_id)
    assert determine(session_id, incident_id)["due_date"] == "2026-10-09"
    warning, overdue = dashboard.NOTICES["warning"][0], dashboard.NOTICES["overdue"][0]

    assert notice_on(session_id, "2026-10-07") is None
    assert notice_on(session_id, "2026-10-08") == warning
    assert notice_on(session_id, "2026-10-09") == warning
    assert notice_on(session_id, "2026-10-10") == overdue

    filed = client.post(
        f"/system/incidents/{incident_id}/filings",
        headers=headers(session_id, "DisclosureApprover"),
        json={"form_type": "8-K Item 1.05", "filing_date": "2026-10-10",
              "accession_number": "0001234567-26-000001", "version": 2},
    )
    assert filed.status_code == 201
    assert notice_on(session_id, "2026-10-10") is None
    assert notice_on(session_id, "2026-10-08") is None
    text = page(session_id)
    assert "8-K Item 1.05 on 2026-10-10 (0001234567-26-000001)" in text
    assert "DisclosureRecorded" in text


def test_the_date_form_names_the_settings_path_and_keeps_the_time_of_day():
    session_id = new_session(test_now="2026-10-05T09:30:00Z")
    text = page(session_id)
    assert f'data-settings="test/sessions/{session_id}/settings"' in text
    assert 'data-time="T09:30:00+00:00"' in text


def test_most_urgent_incident_first():
    session_id = new_session(test_now="2026-09-30T15:00:00Z")
    new_incident(session_id, "Calm", "2026-09-29")
    new_incident(session_id, "Late", "2026-09-01")
    text = page(session_id)
    assert text.index("Late") < text.index("Calm")
    assert dashboard.NOTICES["at_risk"][0] in text


def test_fpi_shows_no_fixed_deadline():
    session_id = new_session(registrant_type="FPI")
    determine(session_id, new_incident(session_id, "Foreign"))
    text = page(session_id)
    assert "6-K" in text and "No fixed deadline" in text


def test_what_it_shows_is_escaped():
    session_id = new_session()
    new_incident(session_id, "<script>alert(1)</script>")
    text = page(session_id)
    assert "<script>alert(1)" not in text and "&lt;script&gt;alert(1)" in text
    missing = client.get("/system/dashboard", params={"session_id": '"><script>alert(2)</script>'})
    assert missing.status_code == 404
    assert "<script>alert(2)" not in missing.text


def test_no_session_shows_only_the_form():
    response = client.get("/system/dashboard")
    assert response.status_code == 200
    assert 'name="session_id"' in response.text and "<table" not in response.text


def test_an_unknown_session_is_a_404_page():
    response = client.get("/system/dashboard", params={"session_id": "pytest-missing"})
    assert response.status_code == 404
    assert "pytest-missing" in response.text


def test_unreachable_storage_is_a_503_page(monkeypatch):
    def unreachable(session_id):
        raise OperationalError("connect", {}, Exception("connection refused"))

    monkeypatch.setattr(dashboard.control, "snapshot", unreachable)
    response = client.get("/system/dashboard", params={"session_id": "pytest-any"})
    assert response.status_code == 503
    assert "connection refused" not in response.text


def test_status_page_links_here():
    assert '<a href="dashboard">' in client.get("/system/").text
