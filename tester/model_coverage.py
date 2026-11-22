"""Trace this suite's evaluated checks to Team 7's versioned milestone design.

This is self-test evidence, not a claim about another team's internal design or about
human-reviewed draft quality. The mapping lists checks the suite does not yet perform too.
"""

import json
from collections.abc import Collection
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DESIGN_VERSION = "team7-phase5-design-v1"
MAPPING_PATH = Path(__file__).with_name("model_coverage.json")
MAPPING_URL = (
    "https://github.com/taran4286/ece30861-f26-team7-tracer"
    "/blob/main/tester/model_coverage.json"
)


def system_identity(value: str) -> tuple[str, str, int, str]:
    """Compare complete system base URLs; host substrings and request headers are not identity."""
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError("Expected an HTTP(S) system base URL without credentials, query or fragment")
    port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname.lower(), port, parts.path.rstrip("/")


def is_own_system(target_url: str, own_system_url: str) -> bool:
    """Only the configured Team 7 system gets model coverage, including a local configured one."""
    if not isinstance(target_url, str) or not isinstance(own_system_url, str):
        return False
    try:
        return system_identity(target_url) == system_identity(own_system_url)
    except ValueError:
        return False


@dataclass(frozen=True)
class DesignRequirement:
    id: str
    test_ids: tuple[str, ...]


@cache
def requirements() -> tuple[DesignRequirement, ...]:
    """Load the versioned mapping on use; reject a broken mapping instead of guessing counts."""
    data = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("design_version") != DESIGN_VERSION:
        raise ValueError("Model mapping has an unexpected design version")
    rows = data.get("requirements")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Model mapping needs a nonempty requirements list")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Model requirements must be objects")
        identifier, tests = row.get("id"), row.get("test_ids")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in seen:
            raise ValueError("Model requirement IDs must be nonempty and unique")
        if (not isinstance(tests, list)
                or any(not isinstance(t, str) or not t.strip() for t in tests)
                or len(tests) != len(set(tests))):
            raise ValueError(f"Invalid test IDs for model requirement {identifier}")
        if not tests and not row.get("evaluation_gap"):
            raise ValueError(f"Unmapped requirement {identifier} must explain its evaluation gap")
        seen.add(identifier)
        result.append(DesignRequirement(identifier, tuple(tests)))
    return tuple(result)


def validate_mapping(planned_ids: Collection[str]) -> None:
    """Catch renamed or removed checks before executing a self-test."""
    mapped = {t for requirement in requirements() for t in requirement.test_ids}
    unknown = mapped - set(planned_ids)
    if unknown:
        raise ValueError(f"Model mapping names unplanned tests: {', '.join(sorted(unknown))}")


def measure(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Count a requirement only after all its mapped checks reached an evaluated outcome.

    Pass and fail both count. Errors, skips and checks prevented by a shared setup/reset
    failure do not. An empty test list identifies an explicit gap, never vacuous coverage.
    """
    evaluated = {
        r["test_id"] for r in results
        if r["outcome"] in {"pass", "fail"}
        and not r["message"].startswith(("Setup failed:", "Before this case,"))
    }
    rows = requirements()
    covered = [r.id for r in rows if r.test_ids and set(r.test_ids) <= evaluated]
    return {
        "covered": len(covered),
        "total": len(rows),
        "basis": (
            f"Design {DESIGN_VERSION}; mapping: {MAPPING_URL}. "
            "All mapped checks must reach pass/fail, excluding setup/reset failures; "
            "unmapped, skipped and error checks do not count. "
            f"Evaluated design requirements: {', '.join(covered) or 'none'}. "
            "Stub checks do not measure real-model or human-review quality."
        ),
    }


def unavailable(reason: str) -> dict[str, Any]:
    """An aborted self-report still identifies its design when measurement is unavailable."""
    return {"covered": None, "total": None,
            "basis": f"Design {DESIGN_VERSION}; mapping: {MAPPING_URL}. Not measured: {reason}"}
