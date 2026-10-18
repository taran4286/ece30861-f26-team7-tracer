"""Shared test data and database checks for the service tests."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from app.clock import utcnow
from app.db import transaction
from app.models import (
    AuditEntry,
    Determination,
    Evidence,
    Incident,
    RegistrantType,
    State,
    TestSession,
)
from app.roles import Role

# The session the acting users below work in; conftest's default_session stores it.
SESSION_ID = f"pytest-{uuid.uuid4().hex}"
# The spec's default test_now, so "today" in that session is 2026-10-05.
TEST_NOW = datetime(2026, 10, 5, 15, tzinfo=UTC)
TODAY = TEST_NOW.date()

RESPONDER = {"user_id": "u-responder", "role": Role.RESPONDER, "session_id": SESSION_ID}
APPROVER = {"user_id": "u-approver", "role": Role.DISCLOSURE_APPROVER, "session_id": SESSION_ID}
BOARD = {"user_id": "u-board", "role": Role.BOARD, "session_id": SESSION_ID}


def new_session(session_id: str | None = None, **settings: Any) -> str:
    """Store a test session with the spec's defaults, overridden by `settings`; returns its ID."""
    session_id = session_id or f"pytest-{uuid.uuid4().hex}"
    defaults = {
        "test_now": TEST_NOW,
        "registrant_type": RegistrantType.DOMESTIC,
        "at_risk_threshold_days": 7,
        "llm_base_url": "https://llm.invalid/v1",
    }
    with transaction() as tx:
        tx.add(TestSession(
            session_id=session_id, created_at=utcnow(), **{**defaults, **settings}
        ))
    return session_id


def in_session(who: dict[str, Any], session_id: str) -> dict[str, Any]:
    """An acting user from above, working in another session."""
    return {**who, "session_id": session_id}


def ticket(n: int = 1, **overrides: Any) -> dict[str, Any]:
    item = {
        "ticket_id": f"TCK-{n}",
        "summary": f"Ticket {n}",
        "created_at": "2026-10-01T12:00:00Z",
    }
    return {**item, **overrides}


def alert(n: int = 1, **overrides: Any) -> dict[str, Any]:
    item = {
        "alert_id": f"ALR-{n}",
        "rule_name": f"Rule {n}",
        "severity": "high",
        "detected_at": "2026-10-01T12:00:00+00:00",
    }
    return {**item, **overrides}


def table_counts() -> dict[str, int]:
    """Row counts of every service table, to check a rejection stored nothing."""
    with transaction() as tx:
        return {
            model.__tablename__: tx.scalar(select(func.count()).select_from(model))
            for model in (TestSession, Incident, Evidence, Determination, AuditEntry)
        }


def audit_entries(target_id: str) -> list[AuditEntry]:
    with transaction() as tx:
        return list(
            tx.scalars(
                select(AuditEntry).where(AuditEntry.target_id == target_id).order_by(AuditEntry.seq)
            )
        )


def set_state(incident_id: str, state: State) -> None:
    """Put an incident in any state directly, until the workflow operations can get it there."""
    with transaction() as tx:
        tx.execute(update(Incident).where(Incident.incident_id == incident_id).values(state=state))
