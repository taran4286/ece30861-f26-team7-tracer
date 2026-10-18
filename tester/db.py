"""The tester's engine, declarative Base, and one transaction per operation.

Mirrors app/db.py, which the tester can't import. It connects as the tester role through
TESTER_DATABASE_URL, so sessions and reports outlive the instance that wrote them.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from tester.config import get_tester_settings


class Base(DeclarativeBase):
    pass


# Same bound as app/db.py: psycopg otherwise waits 130 s on each address a host resolves to.
CONNECT_TIMEOUT_SECONDS = 5


def parse_url(value: str) -> URL:
    """TESTER_DATABASE_URL, raising ArgumentError, a SQLAlchemyError, for any malformed one.

    As in app/db.py: SQLAlchemy raises a plain ValueError for some, such as a port that isn't a
    number, which the routes' storage handlers would miss. The message never includes the URL.
    """
    try:
        return make_url(value)
    except ValueError:
        raise ArgumentError("TESTER_DATABASE_URL is not a valid database URL") from None


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping replaces connections the server dropped while idle, e.g. when Neon pauses.
    return create_engine(
        parse_url(get_tester_settings().tester_database_url),
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
