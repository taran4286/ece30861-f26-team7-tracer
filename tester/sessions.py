"""Tester sessions in Postgres: store a prepared session, start its one run, store and read its report.

Each function is one short transaction, so a run never holds a transaction open while it
calls the target.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from tester.db import transaction
from tester.models import RunStatus, StoredSession


class ControlError(Exception):
    """A test-control failure: its HTTP status and the TestControlError body."""

    def __init__(self, status: int, message: str, source: str = "tester"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.source = source

    def to_dict(self) -> dict[str, str]:
        return {"message": self.message, "source": self.source}


def create_session(session_id: str, target_url: str) -> None:
    """Store a prepared session as ready. Call it only once the target's setup succeeded."""
    with transaction() as tx:
        tx.add(
            StoredSession(
                session_id=session_id,
                target_url=target_url,
                status=RunStatus.READY,
                created_at=datetime.now(UTC),
            )
        )


def start_run(session_id: str) -> str:
    """Mark a ready session running and return its target URL.

    One UPDATE checks and changes the status, so of two calls at once only one starts the
    run. Raises 404 for an unknown session and 409 for one that has already started.
    """
    with transaction() as tx:
        target_url = tx.scalar(
            update(StoredSession)
            .where(StoredSession.session_id == session_id, StoredSession.status == RunStatus.READY)
            .values(status=RunStatus.RUNNING)
            .returning(StoredSession.target_url)
        )
        if target_url is not None:
            return target_url
        if tx.get(StoredSession, session_id) is None:
            raise ControlError(404, f"Unknown session {session_id!r}")
        raise ControlError(409, f"Session {session_id!r} has already run; set up a new session")


def finish_run(session_id: str, report: dict[str, Any], *, aborted: bool = False) -> None:
    """Store a running session's report and mark it finished, or aborted.

    Commits before returning, so the run responds only once its report is stored. Only a
    running session changes, so a stored report is never replaced.
    """
    with transaction() as tx:
        stored = tx.scalar(
            update(StoredSession)
            .where(StoredSession.session_id == session_id, StoredSession.status == RunStatus.RUNNING)
            .values(
                status=RunStatus.ABORTED if aborted else RunStatus.FINISHED,
                report=report,
                finished_at=datetime.now(UTC),
            )
            .returning(StoredSession.session_id)
        )
    if stored is None:
        # Only the run that started the session stores its report, so this is a tester bug.
        raise ControlError(500, f"Session {session_id!r} isn't running, so its report wasn't stored")


def get_report(session_id: str) -> dict[str, Any]:
    """The stored report of a finished or aborted run. Never starts or repeats a run."""
    with transaction() as tx:
        session = tx.get(StoredSession, session_id)
    if session is None:
        raise ControlError(404, f"Unknown session {session_id!r}")
    if session.report is None:
        # Ready or running: the report is stored only when the run ends.
        raise ControlError(409, f"Session {session_id!r} hasn't finished its run")
    return session.report
