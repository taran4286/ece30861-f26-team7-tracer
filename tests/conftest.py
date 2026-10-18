"""Fixtures for the service tests. They run against the real local database as tracer_app."""

from typing import Any

import pytest

from app.services.incidents import create_incident
from tests.helpers import RESPONDER, SESSION_ID, new_session


@pytest.fixture(scope="session")
def default_session() -> str:
    """Stores the session RESPONDER, APPROVER and BOARD work in, once per test run."""
    return new_session(SESSION_ID)


@pytest.fixture
def incident(default_session: str) -> dict[str, Any]:
    return create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"}, **RESPONDER)
