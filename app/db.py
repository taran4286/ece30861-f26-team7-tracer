"""Engine, declarative Base, and one transaction per operation."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine
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


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, json_serializer=_json_dumps)


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
