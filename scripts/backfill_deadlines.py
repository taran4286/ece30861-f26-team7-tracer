"""Backfill missing Domestic Material deadlines without replacing existing data.

Usage: python scripts/backfill_deadlines.py

Reads DATABASE_URL (app role) from the environment or .env. Run it after deploying
deadline support, so determinations recorded before the deploy are filled too. One
transaction locks qualifying rows, calculates all deadlines, and fills only null due
dates. Re-running is safe. It writes no audit entries and leaves versions unchanged.
It never creates tables. Exits 0 on success, 1 on failure with the whole transaction
rolled back.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import transaction  # noqa: E402
from app.models import Decision, Determination, FormType, Incident, RegistrantType, TestSession  # noqa: E402
from app.services.deadlines import disclosure_due_date  # noqa: E402


def backfill_deadlines(tx: Session) -> int:
    """Fill eligible null deadlines in the caller's transaction; return the row count."""
    rows = tx.execute(
        select(Incident, Determination.determination_date)
        .join(Determination, Determination.incident_id == Incident.incident_id)
        .join(TestSession, TestSession.session_id == Incident.session_id)
        .where(
            Incident.due_date.is_(None),
            Incident.required_form == FormType.FORM_8K_ITEM_105,
            Determination.decision == Decision.MATERIAL,
            TestSession.registrant_type == RegistrantType.DOMESTIC,
        )
        .order_by(Incident.incident_id)
        .with_for_update(of=Incident)
    ).all()
    # Calculate everything before assigning fields, including range validation.
    updates = [(incident, disclosure_due_date(incident.required_form, day)) for incident, day in rows]
    for incident, due_date in updates:
        incident.due_date = due_date
    tx.flush()
    return len(updates)


def main() -> int:
    """Apply the atomic data repair and report a count or a sanitized failure."""
    try:
        with transaction() as tx:
            count = backfill_deadlines(tx)
    except Exception as error:
        # Database exceptions can contain connection credentials or row contents.
        print(f"backfill_deadlines: failed ({type(error).__name__}); nothing was changed", file=sys.stderr)
        return 1
    print(f"backfill_deadlines: done; updated {count} incidents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
