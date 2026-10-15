"""Shared test data and database checks for the service tests."""

from typing import Any

from sqlalchemy import func, select, update

from app.db import transaction
from app.models import AuditEntry, Evidence, Incident, State
from app.roles import Role

RESPONDER = {"user_id": "u-responder", "role": Role.RESPONDER}
APPROVER = {"user_id": "u-approver", "role": Role.DISCLOSURE_APPROVER}
BOARD = {"user_id": "u-board", "role": Role.BOARD}


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
    """Row counts of every table Group B writes, to check a rejection stored nothing."""
    with transaction() as tx:
        return {
            model.__tablename__: tx.scalar(select(func.count()).select_from(model))
            for model in (Incident, Evidence, AuditEntry)
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
