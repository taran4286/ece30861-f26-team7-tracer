"""Calendar deadlines from the shared API's business-day convention; no database needed."""

from datetime import date

import pytest

from app.models import FormType
from app.services.deadlines import add_business_days, disclosure_due_date, is_business_day


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
])
def test_four_business_day_deadline(start, expected):
    assert add_business_days(date.fromisoformat(start)) == date.fromisoformat(expected)


@pytest.mark.parametrize("holiday", [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-10-12",
    "2026-11-11", "2026-11-26", "2026-12-25", "2021-12-31",
])
def test_observed_federal_holidays_are_not_business_days(holiday):
    assert not is_business_day(date.fromisoformat(holiday))


@pytest.mark.parametrize("form", list(FormType) + [None])
def test_only_initial_domestic_and_amendment_forms_have_deadlines(form):
    expected = date(2026, 10, 9) if form in {FormType.FORM_8K_ITEM_105, FormType.FORM_8KA} else None
    assert disclosure_due_date(form, date(2026, 10, 5)) == expected


def test_zero_days_and_unsupported_date_range():
    assert add_business_days(date(2026, 10, 3), 0) == date(2026, 10, 3)
    with pytest.raises(OverflowError):
        add_business_days(date.max)


@pytest.mark.parametrize("days", [-1, True, 1.5])
def test_invalid_business_day_counts(days):
    with pytest.raises(ValueError):
        add_business_days(date(2026, 10, 5), days)
