"""Per-session code coverage (TEST-02): the lines of the system's code each session's project
calls ran, counted against every line that could run inside a function.

`start()` turns on `sys.monitoring` LINE events once per process. The API layer opens a
`recording()` around each request that names a session, `claim()` marks it as a project call in
that session, and `save()` stores what it ran. `covered_lines()` reads it back for the report.
"""

import atexit
import dis
import inspect
import sys
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import CodeType

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import transaction
from app.models import CoverageLine

# The system's code: the app package. Lines are keyed by path from the repo root, e.g.
# "app/services/workflow.py", so they don't depend on where the deployment puts the files.
PACKAGE = Path(__file__).resolve().parent
_ROOT = PACKAGE.parent
# The code that measures: neither recorded nor counted.
_MEASURING = frozenset({Path(__file__).resolve(), PACKAGE / "api" / "recording.py"})

# Tried in order; another tool, such as coverage.py, may hold one already.
_TOOL_IDS = (sys.monitoring.COVERAGE_ID, 3, 4)
_TOOL_NAME = "tracer-session-coverage"


class Recording:
    """The lines one request ran, and the session they count for once a project route claims it."""

    __slots__ = ("hits", "session_id", "active")

    def __init__(self) -> None:
        self.hits: set[tuple[str, int]] = set()
        self.session_id: str | None = None
        self.active = True


_current: ContextVar[Recording | None] = ContextVar("coverage_recording", default=None)
# Each code file's key, or None for code outside the app package and the measuring code.
_keys: dict[str, str | None] = {}
_lock = threading.Lock()
# The lines this process has stored, for its most recent SAVED_SESSIONS sessions.
SAVED_SESSIONS = 64
_saved: OrderedDict[str, set[tuple[str, int]]] = OrderedDict()
_tool: int | None = None
_unavailable: str | None = None


def _key(filename: str) -> str | None:
    try:
        path = Path(filename).resolve()
    except (OSError, ValueError):
        return None
    if path in _MEASURING or path.suffix != ".py" or not path.is_relative_to(PACKAGE):
        return None
    return path.relative_to(_ROOT).as_posix()


def _on_line(code: CodeType, line: int) -> object:
    """sys.monitoring's LINE callback: runs before each new line of any code in the process."""
    filename = code.co_filename
    try:
        key = _keys[filename]
    except KeyError:
        key = _keys[filename] = _key(filename)
    if key is None:
        # Never reported again for this location, so other code runs at full speed.
        return sys.monitoring.DISABLE
    recording = _current.get()
    if recording is not None and recording.active:
        recording.hits.add((key, line))
    return None


def start() -> bool:
    """Turn on line events for this process, once; False when they can't be."""
    global _tool, _unavailable
    if _tool is not None:
        return True
    with _lock:
        if _tool is None and _unavailable is None:
            for tool in _TOOL_IDS:
                try:
                    sys.monitoring.use_tool_id(tool, _TOOL_NAME)
                except ValueError:
                    continue
                sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, _on_line)
                sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
                _tool = tool
                # Before shutdown clears this module's globals, which the callback reads.
                atexit.register(_stop)
                break
            else:
                _unavailable = "every sys.monitoring tool ID is in use by another tool"
        return _tool is not None


def _stop() -> None:
    """Turn line events off and release the tool ID."""
    global _tool
    with _lock:
        if _tool is not None:
            sys.monitoring.set_events(_tool, sys.monitoring.events.NO_EVENTS)
            sys.monitoring.register_callback(_tool, sys.monitoring.events.LINE, None)
            sys.monitoring.free_tool_id(_tool)
            _tool = None


def unavailable() -> str | None:
    """Why lines can't be recorded in this process, or None when they can."""
    start()
    return _unavailable


@contextmanager
def recording() -> Iterator[Recording]:
    """Record the lines run in this context until it ends or the recording is stopped."""
    current = Recording()
    token = _current.set(current)
    try:
        yield current
    finally:
        current.active = False
        _current.reset(token)


def claim(session_id: str) -> None:
    """Count the current recording, if any, for a project call in this session."""
    current = _current.get()
    if current is not None:
        current.session_id = session_id


def save(session_id: str, hits: frozenset[tuple[str, int]]) -> None:
    """Store the lines a project call ran for its known session, each once.

    Lines this process already stored for the session are skipped without a query, so most
    calls write nothing. An unknown session stores nothing. Sorted, so concurrent calls in one
    session insert in the same order and can't deadlock.
    """
    # PostgreSQL text can't hold NUL, so no stored session has one.
    if "\x00" in session_id:
        return
    with _lock:
        new = hits - _saved.get(session_id, frozenset())
    if not new:
        return
    paths, lines = zip(*sorted(new))
    with transaction() as tx:
        known = tx.execute(
            text(
                "WITH known AS ("
                "SELECT session_id FROM test_sessions WHERE session_id = :session_id), "
                "stored AS ("
                "INSERT INTO coverage_lines (session_id, path, line) "
                "SELECT known.session_id, hit.path, hit.line FROM known, "
                "unnest(CAST(:paths AS text[]), CAST(:lines AS integer[])) AS hit(path, line) "
                "ON CONFLICT DO NOTHING) "
                "SELECT count(*) FROM known"
            ),
            {"session_id": session_id, "paths": list(paths), "lines": list(lines)},
        ).scalar_one()
    if known:
        with _lock:
            _saved.setdefault(session_id, set()).update(new)
            _saved.move_to_end(session_id)
            while len(_saved) > SAVED_SESSIONS:
                _saved.popitem(last=False)


def covered_lines(tx: Session, session_id: str) -> int:
    """How many of today's executable lines the session's project calls ran.

    Lines stored under an earlier deployment's code that no longer exist aren't counted, so
    covered never exceeds total.
    """
    executable = executable_lines()
    rows = tx.execute(
        select(CoverageLine.path, CoverageLine.line).where(CoverageLine.session_id == session_id)
    )
    return sum(1 for path, line in rows if line in executable.get(path, ()))


@lru_cache
def executable_lines() -> dict[str, frozenset[int]]:
    """Each app file's lines that can run inside a function, from compiling its source.

    Module and class bodies are left out: they run once, at import. So is each function's
    prologue, the def or decorator line, which sys.monitoring never reports.
    """
    files = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.resolve() in _MEASURING:
            continue
        module = compile(path.read_text(encoding="utf-8"), str(path), "exec")
        files[path.relative_to(_ROOT).as_posix()] = frozenset(
            line for code in _functions(module) for line in _body_lines(code)
        )
    return files


def total_lines() -> int:
    return sum(len(lines) for lines in executable_lines().values())


def _functions(code: CodeType) -> Iterator[CodeType]:
    """Every function, lambda and generator expression compiled inside `code`."""
    for const in code.co_consts:
        if inspect.iscode(const):
            if const.co_flags & inspect.CO_NEWLOCALS:
                yield const
            yield from _functions(const)


def _body_lines(code: CodeType) -> Iterator[int]:
    """The lines of a function's own instructions after its first RESUME."""
    started = False
    for instruction in dis.get_instructions(code):
        if instruction.opname == "RESUME":
            started = True
        elif started and instruction.positions and instruction.positions.lineno is not None:
            yield instruction.positions.lineno
