"""Filing body and contextual validation; these checks need no database."""

from copy import deepcopy
from datetime import date

import pytest

from app import db
from app.errors import ErrorCode, TracerError
from app.models import FormType
from app.services.filings import validate_filing_request


TODAY = date(2026, 10, 8)
DETERMINATION_DATE = date(2026, 10, 5)
BODY = {
    "form_type": "8-K Item 1.05",
    "filing_date": "2026-10-08",
    "accession_number": "0001234567-26-000001",
    "version": 2,
}


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """A validation helper must not connect to storage, even on success."""
    def forbidden(*args, **kwargs):
        pytest.fail("Filing validation accessed the database")

    monkeypatch.setattr(db, "get_engine", forbidden)
    monkeypatch.setattr(db, "_session_factory", forbidden)


def validate(body, **context):
    return validate_filing_request(body, **{
        "today": TODAY,
        "determination_date": DETERMINATION_DATE,
        "required_form": FormType.FORM_8K_ITEM_105,
        **context,
    })


@pytest.mark.parametrize("form", ["8-K Item 1.05", "6-K", "8-K/A"])
def test_valid_forms_and_parsed_fields(form):
    body = {**BODY, "form_type": form, "unavailable_elements": ["impact"]}
    if form == "6-K":
        body["home_disclosure_date"] = "2026-10-07"
    previous = deepcopy(body)
    result = validate(body, required_form=FormType(form))
    assert result == {
        "form_type": FormType(form),
        "filing_date": TODAY,
        "accession_number": BODY["accession_number"],
        "version": 2,
        "unavailable_elements": ["impact"],
        "home_disclosure_date": date(2026, 10, 7) if form == "6-K" else None,
    }
    assert body == previous
    result["unavailable_elements"].append("scope")
    assert body == previous


@pytest.mark.parametrize("day", ["2026-10-05", "2026-10-08"])
def test_filing_date_includes_determination_and_today(day):
    assert validate({**BODY, "filing_date": day})["filing_date"] == date.fromisoformat(day)


@pytest.mark.parametrize("field", list(BODY))
def test_missing_required_field(field):
    body = dict(BODY)
    del body[field]
    with pytest.raises(TracerError) as error:
        validate(body)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, field)


@pytest.mark.parametrize("body", [None, [], "filing", 1, True])
def test_requires_json_object(body):
    with pytest.raises(TracerError) as error:
        validate(body)
    assert error.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize("field,value,expected_field", [
    ("form_type", "8-K Item 8.01", "form_type"),
    ("form_type", None, "form_type"),
    ("form_type", [], "form_type"),
    ("filing_date", "2026-10-04", "filing_date"),
    ("filing_date", "2026-10-09", "filing_date"),
    ("filing_date", "2026-02-30", "filing_date"),
    ("filing_date", "20261008", "filing_date"),
    ("filing_date", None, "filing_date"),
    ("accession_number", "123456789-26-000001", "accession_number"),
    ("accession_number", "0001234567-2026-000001", "accession_number"),
    ("accession_number", "0001234567-26-00001", "accession_number"),
    ("accession_number", "0001234567-26-000001\n", "accession_number"),
    ("accession_number", " 0001234567-26-000001", "accession_number"),
    ("accession_number", "\u0660" * 10 + "-26-000001", "accession_number"),
    ("accession_number", None, "accession_number"),
    ("version", True, "version"),
    ("version", 0, "version"),
    ("version", 1.5, "version"),
    ("version", "2", "version"),
    ("unavailable_elements", None, "unavailable_elements"),
    ("unavailable_elements", "impact", "unavailable_elements"),
    ("unavailable_elements", ["impact", "impact"], "unavailable_elements[1]"),
    ("unavailable_elements", ["other"], "unavailable_elements[0]"),
    ("unavailable_elements", [{}], "unavailable_elements[0]"),
    ("home_disclosure_date", "2026-10-09", "home_disclosure_date"),
    ("home_disclosure_date", "2026-02-30", "home_disclosure_date"),
    ("home_disclosure_date", None, "home_disclosure_date"),
])
def test_invalid_field_names_and_unchanged_request(field, value, expected_field):
    body = {**BODY, field: value}
    previous = deepcopy(body)
    with pytest.raises(TracerError) as error:
        validate(body)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, expected_field)
    assert body == previous


def test_form_must_match_existing_required_form():
    with pytest.raises(TracerError) as error:
        validate(BODY, required_form=FormType.FORM_8KA)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, "form_type")


def test_home_disclosure_date_required_for_6k():
    with pytest.raises(TracerError) as error:
        validate({**BODY, "form_type": "6-K"}, required_form=FormType.FORM_6K)
    assert (error.value.code, error.value.field) == (ErrorCode.VALIDATION_ERROR, "home_disclosure_date")


def test_optional_elements_and_home_date():
    result = validate(BODY)
    assert result["unavailable_elements"] == [] and result["home_disclosure_date"] is None
    body = {**BODY, "unavailable_elements": ["nature", "scope", "timing", "impact"],
            "home_disclosure_date": "2026-10-08"}
    assert validate(body)["unavailable_elements"] == body["unavailable_elements"]
    assert validate(body)["home_disclosure_date"] == TODAY


@pytest.mark.parametrize("version", [1, 1.0, 2])
def test_valid_version_shape_without_stale_version_check(version):
    result = validate({**BODY, "version": version})
    assert type(result["version"]) is int and result["version"] == int(version)


def test_no_determination_or_required_form_leaves_state_decision_to_caller():
    assert validate(BODY, determination_date=None, required_form=None)["filing_date"] == TODAY


def test_date_rules_use_supplied_today():
    with pytest.raises(TracerError) as error:
        validate(BODY, today=date(2026, 10, 7))
    assert error.value.field == "filing_date"
