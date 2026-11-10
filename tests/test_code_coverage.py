"""app/code_coverage.py: which lines count, recording them per context, and storing them per
session."""

import threading
import uuid
from contextvars import copy_context
from pathlib import Path

import pytest
from sqlalchemy import select

from app import clock, code_coverage, models
from app.db import transaction
from app.models import CoverageLine, TestSession
from tests.helpers import TEST_NOW, new_session

ROOT = Path(__file__).resolve().parents[1]
MEASURING = {"app/code_coverage.py", "app/api/recording.py"}


def line_of(path: str, text: str) -> int:
    """The number of the one line in a repo file that contains `text`."""
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    matches = [number for number, line in enumerate(lines, start=1) if text in line]
    assert len(matches) == 1, (path, text)
    return matches[0]


UTCNOW = ("app/clock.py", line_of("app/clock.py", "return datetime.now(UTC)"))
TODAY = ("app/clock.py", line_of("app/clock.py", "return session.test_now"))


def stored(session_id: str) -> set[tuple[str, int]]:
    with transaction() as tx:
        rows = tx.execute(
            select(CoverageLine.path, CoverageLine.line).where(CoverageLine.session_id == session_id)
        )
        return {(path, line) for path, line in rows}


@pytest.fixture
def no_database(monkeypatch):
    def forbidden():
        pytest.fail("This call must not reach the database")
    monkeypatch.setattr(code_coverage, "transaction", forbidden)


def test_every_app_module_counts_except_the_measuring_code():
    modules = {path.relative_to(ROOT).as_posix() for path in (ROOT / "app").rglob("*.py")}
    assert set(code_coverage.executable_lines()) == modules - MEASURING
    assert code_coverage.total_lines() == sum(
        len(lines) for lines in code_coverage.executable_lines().values()
    )


def test_only_lines_inside_functions_count():
    # clock.py's imports, def lines and docstrings never run inside a function call.
    assert code_coverage.executable_lines()["app/clock.py"] == {UTCNOW[1], TODAY[1]}
    assert code_coverage.executable_lines()["app/__init__.py"] == frozenset()


def test_a_recording_holds_the_app_lines_run_in_its_context():
    assert code_coverage.start()
    assert code_coverage.unavailable() is None
    with code_coverage.recording() as current:
        clock.utcnow()
        # A thread running in a copy of the context, as FastAPI's thread pool does, records
        # into the same recording; one with its own context doesn't.
        copied = threading.Thread(target=copy_context().run,
                                  args=(clock.today, TestSession(test_now=TEST_NOW)))
        unrelated = threading.Thread(target=models.new_id, args=("x",))
        for thread in (copied, unrelated):
            thread.start()
            thread.join()
    models.new_id("y")
    assert current.hits == {UTCNOW, TODAY}
    assert not current.active


def test_claim_names_the_current_recordings_session():
    code_coverage.claim("pytest-no-recording")
    with code_coverage.recording() as current:
        assert current.session_id is None
        code_coverage.claim("pytest-claimed")
    assert current.session_id == "pytest-claimed"


def test_save_stores_each_line_once_for_its_session():
    session_id, other = new_session(), new_session()
    code_coverage.save(session_id, frozenset({UTCNOW}))
    code_coverage.save(session_id, frozenset({UTCNOW, TODAY}))
    assert stored(session_id) == {UTCNOW, TODAY}
    assert stored(other) == set()


def test_save_skips_lines_this_process_already_stored(monkeypatch):
    session_id = new_session()
    code_coverage.save(session_id, frozenset({UTCNOW}))
    monkeypatch.setattr(code_coverage, "transaction", lambda: pytest.fail("queried again"))
    code_coverage.save(session_id, frozenset({UTCNOW}))
    code_coverage.save(session_id, frozenset())


def test_an_unknown_session_stores_nothing_and_isnt_remembered():
    session_id = f"pytest-{uuid.uuid4().hex}"
    code_coverage.save(session_id, frozenset({UTCNOW}))
    assert stored(session_id) == set()
    # Set up later under the same ID, the session gets the line.
    new_session(session_id)
    code_coverage.save(session_id, frozenset({UTCNOW}))
    assert stored(session_id) == {UTCNOW}


@pytest.mark.usefixtures("no_database")
def test_a_session_id_with_nul_stores_nothing_without_a_query():
    code_coverage.save("pytest-\x00", frozenset({UTCNOW}))


def test_the_process_remembers_only_its_latest_sessions(monkeypatch):
    monkeypatch.setattr(code_coverage, "SAVED_SESSIONS", 1)
    first, second = new_session(), new_session()
    code_coverage.save(first, frozenset({UTCNOW}))
    code_coverage.save(second, frozenset({UTCNOW}))
    assert list(code_coverage._saved) == [second]


def test_covered_lines_counts_only_lines_that_can_run():
    session_id = new_session()
    # clock.py's line 1 is its docstring, and no app/gone.py exists, as after a later deploy.
    code_coverage.save(session_id, frozenset({UTCNOW, TODAY, ("app/clock.py", 1),
                                              ("app/gone.py", 3)}))
    with transaction() as tx:
        assert code_coverage.covered_lines(tx, session_id) == 2
