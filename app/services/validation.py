"""Body validation helpers shared by the services; each raises VALIDATION_ERROR naming the field."""

import re
from datetime import UTC, date, datetime
from typing import Any

from app.errors import ErrorCode, TracerError

# format: date is exactly YYYY-MM-DD; fromisoformat alone also accepts 20261015.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# format: date-time is RFC 3339: a full date, time with seconds, and a Z or ±HH:MM offset.
_DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")


def invalid(field: str | None, message: str) -> TracerError:
    return TracerError(ErrorCode.VALIDATION_ERROR, message, field=field)


def require_object(body: Any, field: str | None = None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise invalid(field, f"{field or 'Request body'} must be an object")
    return body


def require_string(obj: dict[str, Any], key: str, field: str) -> str:
    """A present, non-blank string."""
    value = obj.get(key)
    if value is None:
        raise invalid(field, f"{field} is required")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise invalid(field, f"{field} must be a non-empty string")
    return value


def parse_integer(value: Any, field: str, minimum: int = 1, maximum: int | None = None) -> int:
    """An integer of at least `minimum`, and at most `maximum` when one is given. A whole-number
    float such as 1.0 counts, as in JSON Schema 2020-12; booleans don't, though Python treats
    them as integers. Pass a maximum for a value stored in a column with a size limit."""
    if value is None:
        raise invalid(field, f"{field} is required")
    whole = isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    too_big = maximum is not None and whole and value > maximum
    if isinstance(value, bool) or not whole or value < minimum or too_big:
        bounds = f"from {minimum} to {maximum}" if maximum is not None else f"of at least {minimum}"
        raise invalid(field, f"{field} must be an integer {bounds}")
    return int(value)


def parse_date(value: Any, field: str) -> date:
    """A real YYYY-MM-DD date."""
    if value is None:
        raise invalid(field, f"{field} is required")
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise invalid(field, f"{field} must be a real date in YYYY-MM-DD form")


def parse_date_time(value: Any, field: str) -> datetime:
    """An ISO-8601 (RFC 3339) date-time with an offset, returned in UTC."""
    if value is None:
        raise invalid(field, f"{field} is required")
    if isinstance(value, str) and _DATE_TIME.fullmatch(value):
        try:
            # upper(): RFC 3339 allows a lowercase t and z, which fromisoformat rejects.
            return datetime.fromisoformat(value.upper()).astimezone(UTC)
        except ValueError:
            pass
    raise invalid(field, f"{field} must be an ISO-8601 date-time with a UTC offset")
