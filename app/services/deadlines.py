"""Disclosure deadlines using observed U.S. federal holidays."""

from datetime import date, timedelta
from functools import lru_cache

from holidays import UnitedStates

from app.models import FormType


@lru_cache(maxsize=128)
def federal_holidays(year: int) -> frozenset[date]:
    """Observed federal holidays falling in year, including next New Year's observance."""
    years = [year] if year == date.max.year else [year, year + 1]
    holidays = UnitedStates(years=years, observed=True, expand=False)
    return frozenset(day for day in holidays if day.year == year)


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in federal_holidays(day.year)


def add_business_days(start: date, days: int = 4) -> date:
    """Count business days strictly after start; zero returns start unchanged.

    OverflowError means the resulting deadline is outside Python's date range.
    """
    if isinstance(days, bool) or not isinstance(days, int) or days < 0:
        raise ValueError("days must be a non-negative integer")
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
