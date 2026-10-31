"""Test control (TEST-01, TEST-02, TEST-06, TEST-07): prepare a session, report its coverage,
snapshot it, and change its clock or mutable settings.

These take no identity headers and write no audit entries. The API layer turns their
TracerErrors into the spec's TestControlError ({message, source}).
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any

from rfc3986_validator import validate_rfc3986
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clock import utcnow
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import AuditEntry, Incident, RegistrantType, TestSession
from app.services.incidents import incidents_to_dicts
from app.services.validation import (
    invalid,
    parse_date_time,
    parse_integer,
    require_object,
    require_string,
)
from app.services.workflow import audit_entry_to_dict
from app.sessions import load_session

# The pod defaults an empty setup gets (system spec, "Testing interface update").
DEFAULTS: dict[str, Any] = {
    "test_now": datetime(2026, 10, 5, 15, tzinfo=UTC),
    "registrant_type": RegistrantType.DOMESTIC,
    "at_risk_threshold_days": 7,
    # The deployment's LLM URL. No drafting is built, so it names a host that never resolves.
    "llm_base_url": "https://llm.invalid/v1",
}

# The settings a session's settings update may change; registrant_type stays as setup stored it
# (TEST-07). The app role's column grant on test_sessions covers exactly these.
MUTABLE_SETTINGS = ("test_now", "at_risk_threshold_days", "llm_base_url")

# The largest value a PostgreSQL INTEGER column holds, such as at_risk_threshold_days. A larger
# one would fail on insert, which would answer 500 instead of 400.
INTEGER_MAX = 2**31 - 1

# format: uri is an absolute URI: a scheme, a colon, then no whitespace. _absolute_uri also
# checks it against RFC 3986.
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:\S+")

CODE_COVERAGE_BASIS = "Source lines; this system doesn't measure code coverage per session"


def setup_session(body: Any) -> dict[str, str]:
    """POST /test/setup: store a new, empty session under the supplied ID (TEST-01).

    Validates the whole body first, then refuses an ID already in use with CONFLICT.
    """
    body = require_object(body)
    session_id = require_string(body, "session_id", "session_id")
    settings = {**DEFAULTS, **_settings(require_object(body.get("setup"), "setup"))}
    with transaction() as tx:
        if tx.get(TestSession, session_id) is not None:
            raise _duplicate(session_id)
        tx.add(TestSession(session_id=session_id, created_at=utcnow(), **settings))
        try:
            tx.flush()
        except IntegrityError:
            # Another setup stored the same ID after the check above.
            raise _duplicate(session_id) from None
    return {"session_id": session_id}


def _settings(setup: dict[str, Any]) -> dict[str, Any]:
    """The SetupSettings that were sent, checked; additionalProperties is false."""
    for key in setup:
        if key not in DEFAULTS:
            raise invalid(f"setup.{key}", f"Unknown setting {key!r}")
    return {key: _CHECKS[key](setup[key], f"setup.{key}") for key in DEFAULTS if key in setup}


def _registrant_type(value: Any, field: str) -> RegistrantType:
    if not isinstance(value, str) or value not in {r.value for r in RegistrantType}:
        raise invalid(field, "Expected Domestic or FPI")
    return RegistrantType(value)


def _absolute_uri(value: Any, field: str) -> str:
    # _URI asks for something after the scheme's colon, which RFC 3986 alone doesn't; the RFC
    # check rejects what _URI lets through, such as a malformed escape like %ZZ or a NUL.
    if (not isinstance(value, str) or not _URI.fullmatch(value)
            or not validate_rfc3986(value, rule="URI")):
        raise invalid(field, "Expected an absolute URI")
    return value


# Each setting's check, called with the value sent and the field to name in a 400; it returns
# the value to store. Setup and the settings update both use these, so they accept the same
# values.
_CHECKS: dict[str, Callable[[Any, str], Any]] = {
    "test_now": parse_date_time,
    "registrant_type": _registrant_type,
    "at_risk_threshold_days": partial(parse_integer, maximum=INTEGER_MAX),
    "llm_base_url": _absolute_uri,
}


def _duplicate(session_id: str) -> TracerError:
    return TracerError(ErrorCode.CONFLICT, f"Session {session_id!r} already exists")


def coverage_report(session_id: Any) -> dict[str, Any]:
    """GET /test/report?session_id=: code coverage for a session (TEST-02).

    Coverage isn't measured, so the counts are null and `basis` says why, as the spec allows.
    """
    _require_session_id(session_id)
    with transaction() as tx:
        load_session(tx, session_id)
    unmeasured = {"covered": None, "total": None, "basis": CODE_COVERAGE_BASIS}
    return {"session_id": session_id, "coverage": {"code": unmeasured}}


def _require_session_id(session_id: Any) -> None:
    """A 400 for a missing or blank session ID, before any database access."""
    if not isinstance(session_id, str) or not session_id.strip():
        raise invalid("session_id", "session_id is required")


def update_settings(session_id: Any, body: Any) -> dict[str, Any]:
    """PATCH /test/sessions/{session_id}/settings: change a session's clock or mutable settings
    (TEST-07) and return its TestRun.

    Checks the ID and the whole body before any database access. Omitted settings keep their
    values, so an empty body changes nothing; registrant_type can't be sent.
    """
    _require_session_id(session_id)
    body = require_object(body)
    for key in body:
        if key == "registrant_type":
            raise invalid(key, "registrant_type is fixed for the session and can't be changed")
        if key not in MUTABLE_SETTINGS:
            raise invalid(key, f"Unknown setting {key!r}")
    changes = {key: _CHECKS[key](body[key], key) for key in MUTABLE_SETTINGS if key in body}
    with transaction() as tx:
        # Locked, so concurrent updates apply one after the other. The ORM writes only the
        # changed columns, the ones the app role's column grant allows.
        session = load_session(tx, session_id, for_update=True)
        for key, value in changes.items():
            setattr(session, key, value)
        return run_to_dict(session)


def snapshot(run_id: str) -> dict[str, Any]:
    """GET /test/report/{run_id}: the session's settings, incidents and audit trail (TEST-06).

    Testers compare it before and after a rejected call to check that nothing was stored.
    """
    with transaction() as tx:
        session = load_session(tx, run_id)
        incidents = tx.scalars(
            select(Incident).where(Incident.session_id == run_id).order_by(Incident.incident_id)
        ).all()
        entries = tx.scalars(
            select(AuditEntry).where(AuditEntry.session_id == run_id).order_by(AuditEntry.seq)
        ).all()
        return {
            "run": run_to_dict(session),
            "incidents": incidents_to_dicts(tx, incidents),
            # Groups (REQ-04) aren't built.
            "groups": [],
            "audit": [audit_entry_to_dict(entry) for entry in entries],
        }


def run_to_dict(session: TestSession) -> dict[str, Any]:
    """The TestRun schema; run_id is the session ID, and no nice-to-have requirement is built."""
    return {
        "run_id": session.session_id,
        "test_now": session.test_now.astimezone(UTC).isoformat(),
        "registrant_type": session.registrant_type.value,
        "at_risk_threshold_days": session.at_risk_threshold_days,
        "llm_base_url": session.llm_base_url,
        "implemented_optional": [],
    }
