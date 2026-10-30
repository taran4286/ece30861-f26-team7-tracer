"""Validate filing requests without reading or changing stored records."""

import re
from datetime import date
from typing import Any

from app.models import FormType
from app.services.validation import invalid, parse_date, parse_integer, require_object


FILING_FORMS = frozenset({FormType.FORM_8K_ITEM_105, FormType.FORM_6K, FormType.FORM_8KA})
FILING_ELEMENTS = ("nature", "scope", "timing", "impact")
_ACCESSION_NUMBER = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


def validate_filing_request(
    body: Any,
    *,
    today: date,
    determination_date: date | None,
    required_form: FormType | None,
) -> dict[str, Any]:
    """Return checked fields with parsed dates, form and version, or a field-level error.

    The caller supplies the session's today and the incident's existing values.
    An incident without a determination or required form still gets body checks;
    the caller handles whether its state allows filing. No database is accessed.
    """
    body = require_object(body)
    value = body.get("form_type")
    if not isinstance(value, str) or value not in FILING_FORMS:
        raise invalid("form_type", "Expected 8-K Item 1.05, 6-K or 8-K/A")
    form = FormType(value)

    filing_date = parse_date(body.get("filing_date"), "filing_date")
    if filing_date > today:
        raise invalid("filing_date", "filing_date must not be after today")
    if determination_date is not None and filing_date < determination_date:
        raise invalid("filing_date", "filing_date must not be before the determination")

    accession = body.get("accession_number")
    if not isinstance(accession, str) or not _ACCESSION_NUMBER.fullmatch(accession):
        raise invalid("accession_number", "Expected an accession number in 0000000000-00-000000 form")
    # This checks the value's type and range, not whether the client's version is stale.
    version = parse_integer(body.get("version"), "version")

    elements = _unavailable_elements(body.get("unavailable_elements", []))
    home_date = None
    if form == FormType.FORM_6K or "home_disclosure_date" in body:
        home_date = parse_date(body.get("home_disclosure_date"), "home_disclosure_date")
        if home_date > today:
            raise invalid("home_disclosure_date", "home_disclosure_date must not be after today")

    if required_form is not None and form != required_form:
        raise invalid("form_type", "form_type must match the incident's required_form")
    return {
        "form_type": form,
        "filing_date": filing_date,
        "accession_number": accession,
        "version": version,
        "unavailable_elements": elements,
        "home_disclosure_date": home_date,
    }


def _unavailable_elements(value: Any) -> list[str]:
    """Check element names and duplicates; an empty list is valid."""
    if not isinstance(value, list):
        raise invalid("unavailable_elements", "Expected an array of element names")
    for index, element in enumerate(value):
        field = f"unavailable_elements[{index}]"
        if not isinstance(element, str) or element not in FILING_ELEMENTS:
            raise invalid(field, "Expected nature, scope, timing or impact")
        if element in value[:index]:
            raise invalid(field, f"{element} appears more than once")
    # Keep the parsed result separate from the request's mutable array.
    return list(value)
