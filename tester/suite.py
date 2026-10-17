"""Runs planned cases in order and turns each into one TestResult.

A case is a function that takes the Run and returns its pass message. It raises Fail when
the target's answer breaks the pass rule, TargetError when the answer can't be evaluated,
and Skip when it isn't run; any other exception is a tester fault.
"""

from collections.abc import Callable, Iterable
from typing import Any

from tester.client import Actor, Reply, Target, TargetError


class Fail(Exception):
    """The target answered, and the answer breaks the case's pass rule."""


class Skip(Exception):
    """The case is planned but not run."""


class Run:
    """One session's state, shared by the cases in order."""

    def __init__(self, target: Target):
        self.target = target
        self._setups: dict[str, tuple[bool, Any]] = {}
        self._baseline: dict[str, Any] | None = None

    def call(self, method: str, template: str, actor: Actor | None, **kwargs: Any) -> Reply:
        # Any call may change stored data, so it ends the saved baseline.
        self._baseline = None
        return self.target.call(method, template, actor, **kwargs)

    def once(self, key: str, build: Callable[[], Any]) -> Any:
        """Build a shared setup on first use; a failed build fails every case that needs it."""
        if key not in self._setups:
            try:
                self._setups[key] = (True, build())
            except Exception as exc:
                self._setups[key] = (False, exc)
        ok, value = self._setups[key]
        if ok:
            return value
        if isinstance(value, Fail):
            raise Fail(f"Setup failed: {value}")
        if isinstance(value, TargetError):
            raise TargetError(value.source, f"Setup failed: {value.message}")
        raise value

    def forget(self, key: str) -> None:
        """Drop a setup whose records no longer match it, so the next case builds a new one."""
        self._setups.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        return expect(self.target.snapshot(), 200)

    def baseline(self) -> dict[str, Any]:
        """A snapshot that matches stored data now; reused until the next call."""
        if self._baseline is None:
            self._baseline = self.snapshot()
        return self._baseline

    def unchanged(self, before: dict[str, Any], label: str) -> None:
        """Fail unless the session's snapshot still equals `before`."""
        after = self.snapshot()
        if _normalized(after) != _normalized(before):
            raise Fail(f"{label} changed stored data: {_difference(before, after)}")
        self._baseline = after


Case = tuple[str, Callable[[Run], str]]


def expect(reply: Reply, status: int, error: str | None = None, field: str | None = None) -> Any:
    """Return the reply's body if it has this status, error code and field; raise otherwise.

    A 5xx other than 501 is a system failure. A 501 is a route the case needs but the target
    hasn't built, which fails the case. A body that breaks its spec schema fails it too.
    """
    if reply.status != status:
        if reply.status >= 500 and reply.status != 501:
            raise TargetError("system", f"{reply.label} returned {reply.status}{reply.detail()}")
        raise Fail(f"{reply.label}: expected {status}, got {reply.status}{reply.detail()}")
    if reply.schema_errors:
        raise Fail(f"{reply.label}: {status} body breaks the spec schema: {reply.schema_errors[0]}")
    body = reply.body if isinstance(reply.body, dict) else {}
    if error is not None and body.get("error") != error:
        raise Fail(f"{reply.label}: expected error {error}, got {body.get('error')}")
    if field is not None and not field_matches(body.get("field"), field):
        raise Fail(f"{reply.label}: expected field {field!r}, got {body.get('field')!r}")
    return reply.body


def field_matches(actual: Any, expected: str) -> bool:
    # A team may name a part of the field, e.g. evidence_refs[0] for evidence_refs, or
    # items[0].ticket_id for items[0].
    return isinstance(actual, str) and (
        actual == expected or actual.startswith((f"{expected}[", f"{expected}."))
    )


def _normalized(snapshot: dict[str, Any]) -> dict[str, Any]:
    # The spec doesn't order snapshot arrays, so compare them sorted by ID.
    incidents = [
        {**i, "evidence": sorted(i["evidence"], key=lambda e: e["evidence_id"])}
        for i in snapshot["incidents"]
    ]
    return {
        "run": snapshot["run"],
        "incidents": sorted(incidents, key=lambda i: i["incident_id"]),
        "groups": sorted(snapshot["groups"], key=lambda g: g["group_id"]),
        "audit": sorted(snapshot["audit"], key=lambda a: a["entry_id"]),
    }


def _difference(before: dict[str, Any], after: dict[str, Any]) -> str:
    for key in ("incidents", "groups", "audit"):
        if len(before[key]) != len(after[key]):
            return f"{key} went from {len(before[key])} to {len(after[key])}"
    old = {i["incident_id"]: i for i in _normalized(before)["incidents"]}
    for incident in _normalized(after)["incidents"]:
        if old.get(incident["incident_id"]) != incident:
            return f"incident {incident['incident_id']} changed"
    return "audit entries, groups or run settings changed"


def run_cases(cases: Iterable[Case], target: Target) -> list[dict[str, Any]]:
    """Run every case in order and return one TestResult per case."""
    run = Run(target)
    results = []
    for test_id, case in cases:
        try:
            result = {"test_id": test_id, "outcome": "pass", "message": case(run)}
        except Skip as exc:
            result = {"test_id": test_id, "outcome": "skip", "message": str(exc)}
        except Fail as exc:
            result = {"test_id": test_id, "outcome": "fail", "message": str(exc)}
        except TargetError as exc:
            result = {"test_id": test_id, "outcome": "error", "message": exc.message,
                      "source": exc.source}
        except Exception as exc:
            result = {"test_id": test_id, "outcome": "error", "source": "tester",
                      "message": f"Tester fault: {type(exc).__name__}: {exc}"}
        results.append(result)
    return results
