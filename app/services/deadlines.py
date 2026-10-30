"""Disclosure deadlines using observed U.S. federal holidays."""

from datetime import date, timedelta
from functools import lru_cache

from holidays import UnitedStates

from app.models import FormType

_CALENDAR = UnitedStates(observed=True, expand=False)
MIN_CALENDAR_YEAR = _CALENDAR.start_year
MAX_CALENDAR_YEAR = _CALENDAR.end_year


def _check_year(year: int) -> None:
    """Reject years for which the library cannot supply a complete calendar."""
    if not MIN_CALENDAR_YEAR <= year <= MAX_CALENDAR_YEAR:
        raise OverflowError(
            f"Holiday calendar supports years {MIN_CALENDAR_YEAR}–{MAX_CALENDAR_YEAR}"
        )


@lru_cache(maxsize=128)
def federal_holidays(year: int) -> frozenset[date]:
    """Observed federal holidays falling in year, including next New Year's observance."""
    _check_year(year)
    years = range(year, min(year + 1, MAX_CALENDAR_YEAR) + 1)
    holidays = UnitedStates(years=years, observed=True, expand=False)
    return frozenset(day for day in holidays if day.year == year)


def is_business_day(day: date) -> bool:
    """Whether a supported date is a weekday outside the federal holiday calendar."""
    _check_year(day.year)
    return day.weekday() < 5 and day not in federal_holidays(day.year)


def add_business_days(start: date, days: int = 4) -> date:
    """Count business days strictly after start; zero returns start unchanged.

    OverflowError means the start or resulting deadline is outside the holiday calendar range.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative integer")
    _check_year(start.year)
    result = start
    while days:
        result += timedelta(days=1)
        if is_business_day(result):
            days -= 1
    return result


def disclosure_due_date(required_form: FormType | None, start: date) -> date | None:
    """Initial 8-K and amendment deadlines; 6-K and voluntary 8-K have no fixed deadline."""
    if required_form in {FormType.FORM_8K_ITEM_105, FormType.FORM_8KA}:
        return add_business_days(start)
    return None
