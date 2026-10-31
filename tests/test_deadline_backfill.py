"""Deadline upgrade behavior against real PostgreSQL."""

from datetime import date

import pytest

from app.db import transaction
from app.models import Incident, RegistrantType
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from app.services.workflow import record_determination
from scripts.backfill_deadlines import backfill_deadlines
from tests.helpers import APPROVER, BOARD, RESPONDER, in_session, new_session, table_counts, ticket

pytestmark = pytest.mark.usefixtures("default_session")


def test_backfill_preserves_existing_data_and_is_repeatable():
    """Repair legacy Domestic Material rows only, without changing versions or audits."""
    cases = []
    for registrant, decision, legacy in [
        (RegistrantType.DOMESTIC, "Material", True),
        (RegistrantType.DOMESTIC, "Material", False),
        (RegistrantType.FPI, "Material", True),
        (RegistrantType.DOMESTIC, "NotMaterial", True),
    ]:
        session_id = new_session(registrant_type=registrant)
        responder, approver, board = (in_session(who, session_id) for who in (RESPONDER, APPROVER, BOARD))
        incident = create_incident({"title": "Legacy incident", "discovery_date": "2026-10-01"}, **responder)
        ident = incident["incident_id"]
        evidence = import_evidence(ident, {"source": "ticketing", "items": [ticket()]}, **responder)
        result = record_determination(ident, {
            "decision": decision, "determination_date": "2026-10-01", "rationale": "Assessment",
            "evidence_refs": [evidence["evidence"][0]["evidence_id"]], "version": 1,
        }, **approver)
        if legacy:
            with transaction() as tx:
                tx.get(Incident, ident).due_date = None
        elif result["due_date"]:
            # Preserve even an existing value that differs from the current calculation.
            with transaction() as tx:
                tx.get(Incident, ident).due_date = date(2026, 10, 8)
            result["due_date"] = "2026-10-08"
        cases.append((ident, board, result))
    counts = table_counts()
    # The backfill covers the whole database, so check only this test's incidents.
    for _ in range(2):
        with transaction() as tx:
            backfill_deadlines(tx)
        for ident, board, expected in cases:
            assert get_incident(ident, **board) == expected
        assert table_counts() == counts
