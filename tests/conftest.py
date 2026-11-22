"""Fixtures for the service tests. They run against the real local database as tracer_app."""

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import control
from app.services.incidents import create_incident
from tests.helpers import RESPONDER, SESSION_ID, new_session


@pytest.fixture(autouse=True)
def configured_llm(monkeypatch):
    """Set the deployment LLM an empty setup defaults to (control.default_settings); none
    unless a test calls this, whatever the environment or .env holds."""
    def configure(trusted: str | None) -> None:
        monkeypatch.setattr(control, "get_settings",
                            lambda: SimpleNamespace(llm_trusted_base_url=trusted))
    configure(None)
    return configure


@pytest.fixture(scope="session")
def default_session() -> str:
    """Stores the session RESPONDER, APPROVER and BOARD work in, once per test run."""
    return new_session(SESSION_ID)


@pytest.fixture
def incident(default_session: str) -> dict[str, Any]:
    return create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"}, **RESPONDER)
