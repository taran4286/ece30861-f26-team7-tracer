"""Engine, declarative Base, and one transaction per operation."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _json_default(value: Any) -> Any:
    # Lets audit before/after and raw_item hold dates and datetimes directly.
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


# Seconds psycopg waits on each address a host resolves to while connecting (its default
# is 130). Neon hosts resolve to three, so a silent database fails in about 15 s.
CONNECT_TIMEOUT_SECONDS = 5


def parse_url(value: str, setting: str) -> URL:
    """A database URL, raising ArgumentError, a SQLAlchemyError, for any malformed one.

    SQLAlchemy raises a plain ValueError for some, such as a port that isn't a number, which
    handlers that catch SQLAlchemyError for an unreachable database would miss. The message
    names the setting, never the URL, which holds a password.
    """
    try:
        return make_url(value)
    except ValueError:
        raise ArgumentError(f"{setting} is not a valid database URL") from None


@lru_cache
def get_engine() -> Engine:
    # pool_pre_ping replaces connections the server dropped while idle, e.g. when Neon pauses.
    return create_engine(
        parse_url(get_settings().database_url, "DATABASE_URL"),
        json_serializer=_json_dumps,
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def transaction() -> Iterator[Session]:
    """Run one operation in one transaction: commit on success, roll back on any error.

    Usage:
        with transaction() as tx:
            tx.add(...)
            audit(tx, ...)
    """
    with _session_factory()() as session, session.begin():
        yield session
