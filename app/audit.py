"""The audit() helper: one append-only entry, written in the caller's transaction."""

from typing import Any

from sqlalchemy.orm import Session

from app.clock import utcnow
from app.models import AuditAction, AuditEntry, new_id


def audit(
    tx: Session,
    action: AuditAction,
    target_id: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    user_id: str,
) -> AuditEntry:
    """Record one audit entry inside `tx`, so it commits or rolls back with the change.

    target_id is the incident ID. Include `version` in before/after for incident changes.
    """
    entry = AuditEntry(
        entry_id=new_id("aud"),
        user_id=user_id,
        timestamp=utcnow(),
        action=action,
        target_id=target_id,
        before=before,
        after=after,
    )
    tx.add(entry)
    tx.flush()
    return entry
