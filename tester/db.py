"""The tester's engine, declarative Base, and one transaction per operation.

Mirrors app/db.py, which the tester can't import. It connects as the tester role through
TESTER_DATABASE_URL, so sessions and reports outlive the instance that wrote them.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from tester.config import get_tester_settings


class Base(DeclarativeBase):
    pass


# Same bound as app/db.py: psycopg otherwise waits 130 s on each address a host resolves to.
CONNECT_TIMEOUT_SECONDS = 5


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping replaces connections the server dropped while idle, e.g. when Neon pauses.
    return create_engine(
        get_tester_settings().tester_database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def transaction() -> Iterator[Session]:
    """Run one storage operation in one transaction: commit on success, roll back on any error."""
    with _session_factory()() as session, session.begin():
        yield session
