"""Calendar deadlines from the shared API's business-day convention; no database needed."""

from bisect import bisect_right
from datetime import date, timedelta

import pytest
from holidays import UnitedStates

from app.models import FormType
from app.services.deadlines import (
    MAX_CALENDAR_YEAR, MIN_CALENDAR_YEAR, add_business_days,
    disclosure_due_date, federal_holidays, is_business_day,
)


@pytest.mark.parametrize("start,expected", [
    ("2026-10-05", "2026-10-09"),  # Start Monday is not counted.
    ("2026-10-02", "2026-10-08"),  # Weekend skipped.
    ("2026-10-03", "2026-10-08"),  # Weekend start.
    ("2026-10-08", "2026-10-15"),  # Columbus Day.
    ("2026-11-09", "2026-11-16"),  # Veterans Day.
    ("2026-11-24", "2026-12-01"),  # Thanksgiving; Friday is a business day.
    ("2026-07-01", "2026-07-08"),  # July 4 observed Friday July 3.
    ("2027-07-01", "2027-07-08"),  # July 4 observed Monday July 5.
    ("2021-12-29", "2022-01-05"),  # Next year's New Year observed December 31.
    ("2026-12-23", "2026-12-30"),  # Christmas.
    ("2026-12-30", "2027-01-06"),  # Year boundary.
    ("2024-02-28", "2024-03-05"),  # Leap day is a business day.
    ("2026-06-17", "2026-06-24"),  # Juneteenth.
    ("2027-06-17", "2027-06-24"),  # Saturday Juneteenth observed Friday June 18 (AC-11).
])
def test_four_business_day_deadline(start, expected):
    """Match the fixed acceptance examples."""
    assert add_business_days(date.fromisoformat(start)) == date.fromisoformat(expected)


# Independent reference: precompute an ordered business-day calendar, then select the fourth
# eligible day after each start. Include 2028 for deadlines crossing the end of 2027 and for
# New Year's Day 2028, observed on December 31, 2027. No production calendar helpers are used.
_REFERENCE_HOLIDAYS = UnitedStates(years=[2026, 2027, 2028], observed=True, expand=False)
_FIRST_START = date(2026, 1, 1)
_END_STARTS = date(2028, 1, 1)
_REFERENCE_END = date(2028, 1, 15)
_REFERENCE_BUSINESS_DAYS = tuple(
    day for offset in range((_REFERENCE_END - _FIRST_START).days)
    if (day := _FIRST_START + timedelta(days=offset)).weekday() < 5
    and day not in _REFERENCE_HOLIDAYS
)


@pytest.mark.parametrize("start", [
    _FIRST_START + timedelta(days=offset)
    for offset in range((_END_STARTS - _FIRST_START).days)
], ids=lambda day: day.isoformat())
def test_every_start_date_in_2026_and_2027_matches_reference(start):
    """Compare each AC-11 start date against an independent calendar lookup."""
    expected = _REFERENCE_BUSINESS_DAYS[bisect_right(_REFERENCE_BUSINESS_DAYS, start) + 3]
    actual = add_business_days(start)
    assert actual == expected, f"start={start}, expected deadline={expected}, actual deadline={actual}"


@pytest.mark.parametrize("holiday", [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12",
    "2026-11-11", "2026-11-26", "2026-12-25", "2021-12-31",
    "2027-12-24", "2027-12-31",  # Christmas and New Year's Day 2028, both observed Friday.
])
def test_observed_federal_holidays_are_not_business_days(holiday):
    """Exclude observed holidays, including observances in the preceding year."""
    assert not is_business_day(date.fromisoformat(holiday))


@pytest.mark.parametrize("form", list(FormType) + [None])
def test_only_initial_domestic_and_amendment_forms_have_deadlines(form):
    """Apply a fixed deadline only to the two eligible Domestic forms."""
    expected = date(2026, 10, 9) if form in {FormType.FORM_8K_ITEM_105, FormType.FORM_8KA} else None
    assert disclosure_due_date(form, date(2026, 10, 5)) == expected


def test_zero_days_and_unsupported_date_range():
    """Keep supported zero-day starts unchanged and reject unrepresentable deadlines."""
    assert add_business_days(date(2026, 10, 3), 0) == date(2026, 10, 3)
    with pytest.raises(OverflowError):
        add_business_days(date.max)


@pytest.mark.parametrize("days", [-1, True, 1.5])
def test_invalid_business_day_counts(days):
    """Reject negative, boolean and fractional day counts."""
    with pytest.raises(ValueError):
        add_business_days(date(2026, 10, 5), days)


@pytest.mark.parametrize("day", [date(MIN_CALENDAR_YEAR - 1, 12, 31), date(MAX_CALENDAR_YEAR + 1, 1, 1)])
def test_unsupported_calendar_dates_are_rejected(day):
    """Never treat a year without library coverage as having no holidays."""
    for operation in (is_business_day, add_business_days):
        with pytest.raises(OverflowError, match="Holiday calendar supports"):
            operation(day)
    with pytest.raises(OverflowError, match="Holiday calendar supports"):
        federal_holidays(day.year)


def test_deadline_cannot_cross_calendar_boundary():
    """Reject even a supported start when its fourth business day is unsupported."""
    with pytest.raises(OverflowError, match="Holiday calendar supports"):
        add_business_days(date(MAX_CALENDAR_YEAR, 12, 30))


def test_last_supported_year_keeps_next_new_year_observance():
    """The last supported year keeps a Friday observance of the next New Year's Day."""
    assert add_business_days(date(MAX_CALENDAR_YEAR, 12, 1)).year == MAX_CALENDAR_YEAR
    if date(MAX_CALENDAR_YEAR + 1, 1, 1).weekday() != 5:
        pytest.skip(f"New Year's Day {MAX_CALENDAR_YEAR + 1} isn't a Saturday, so nothing is observed early")
    assert not is_business_day(date(MAX_CALENDAR_YEAR, 12, 31))
