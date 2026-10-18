"""Checks JSON against the schemas in our copy of system-openapi.yaml."""

import re
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC = Path(__file__).resolve().parents[1] / "system-openapi.yaml"
SPEC_URI = "urn:tracer-system-openapi"

DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})")

FORMATS = FormatChecker(formats=())


@FORMATS.checks("date", raises=ValueError)
def _is_date(value: object) -> bool:
    # The spec's dates are YYYY-MM-DD; fromisoformat alone also takes forms like 20261005.
    if not isinstance(value, str):
        return True
    return DATE.fullmatch(value) is not None and bool(date.fromisoformat(value))


@FORMATS.checks("date-time", raises=ValueError)
def _is_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return True
    return DATE_TIME.fullmatch(value) is not None and bool(datetime.fromisoformat(value.upper()))


@cache
def _spec() -> dict[str, Any]:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


@cache
def _validator(pointer: str) -> Draft202012Validator:
    # The spec is one resource, so its internal #/components/... refs resolve against it.
    registry = Registry().with_resource(SPEC_URI, Resource(_spec(), DRAFT202012))
    return Draft202012Validator(
        {"$ref": f"{SPEC_URI}#{quote(pointer, safe='/~')}"},
        registry=registry,
        format_checker=FORMATS,
    )


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _errors(pointer: str, instance: Any) -> list[str]:
    errors = sorted(_validator(pointer).iter_errors(instance), key=lambda e: list(e.absolute_path))
    return [f"{e.json_path}: {e.message}" for e in errors]


def schema_errors(name: str, instance: Any) -> list[str]:
    """Problems with `instance` against components.schemas[name]; empty when it's valid."""
    return _errors(f"/components/schemas/{_escape(name)}", instance)


def response_errors(method: str, template: str, status: int, body: Any) -> list[str]:
    """Problems with a response body against the spec's schema for its operation and status.

    Empty when the body is valid or the spec gives no JSON schema for that status.
    """
    operation = _spec()["paths"].get(template, {}).get(method.lower(), {})
    responses = operation.get("responses", {})
    key = str(status) if str(status) in responses else "default"
    if key not in responses:
        return []
    response = responses[key]
    pointer = f"/paths/{_escape(template)}/{method.lower()}/responses/{key}"
    if "$ref" in response:
        pointer = response["$ref"].removeprefix("#")
        response = _spec()["components"]["responses"][pointer.rsplit("/", 1)[1]]
    if "application/json" not in response.get("content", {}):
        return []
    return _errors(f"{pointer}/content/application~1json/schema", body)
