"""The tester's own business-day calendar, for the due dates it expects.

Business days exclude weekends and observed U.S. federal holidays, and the start day is not
counted (the system spec's conventions). The calendar comes from the `holidays` library, never
from the system under test.
"""

from datetime import date, timedelta
from functools import cache

import holidays


@cache
def _holidays(year: int) -> frozenset[date]:
    # A year's calendar includes the next New Year's Day when it's observed on December 31.
    return frozenset(holidays.UnitedStates(years=year, observed=True))


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _holidays(day.year)


def add_business_days(start: date, days: int = 4) -> date:
    """The `days`th business day after `start`."""
    day = start
    while days:
        day += timedelta(days=1)
        if is_business_day(day):
            days -= 1
    return day
