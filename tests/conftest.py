"""Fixtures for the service tests. They run against the real local database as tracer_app."""

from typing import Any

import pytest

from app.services.incidents import create_incident
from tests.helpers import RESPONDER


@pytest.fixture
def incident() -> dict[str, Any]:
    return create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"}, **RESPONDER)
