"""HTTP access to one target system for one test session."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2

from tester.spec import response_errors

# Each project call waits this long, with no retry (Tester API description).
CALL_TIMEOUT = 10.0
NO_BODY = object()


@dataclass(frozen=True)
class Actor:
    """The identity headers for one call; None leaves that header out."""

    role: str | None
    user: str | None


class TargetError(Exception):
    """The target's reply can't be evaluated; `source` says where it went wrong."""

    def __init__(self, source: str, message: str):
        super().__init__(message)
        self.source = source
        self.message = message


@dataclass
class Reply:
    label: str
    status: int
    headers: httpx2.Headers
    body: Any
    schema_errors: list[str] = field(default_factory=list)

    def detail(self) -> str:
        """The error code and message of an Error body, for test messages."""
        if isinstance(self.body, dict) and "error" in self.body:
            return f" {self.body.get('error')}: {self.body.get('message')}"
        return ""


class Target:
    """Sends project and test-control calls to a system base URL as one session.

    `http` is an httpx2 client whose base URL is the system base URL. Every reply is
    checked against the spec's schema for its operation and status.
    """

    def __init__(self, http: httpx2.Client, session_id: str):
        self.http = http
        self.session_id = session_id

    def call(
        self,
        method: str,
        template: str,
        actor: Actor | None,
        *,
        json: Any = NO_BODY,
        **path: str,
    ) -> Reply:
        """Send one call; `template` is the spec path, filled from `path`.

        Raises TargetError("communication") when no response arrives.
        """
        label = f"{method} {template}"
        url = template.format(**{k: quote(str(v), safe="") for k, v in path.items()})
        headers = {}
        if actor is not None:
            # Project calls carry the session; test-control calls take no identity headers.
            headers["X-Test-Run"] = self.session_id
            if actor.user is not None:
                headers["X-User-Id"] = actor.user
            if actor.role is not None:
                headers["X-User-Role"] = actor.role
        kwargs: dict[str, Any] = {"headers": headers, "timeout": CALL_TIMEOUT}
        if json is not NO_BODY:
            kwargs["json"] = json
        try:
            # httpx2 appends a relative URL to the base URL's path, /system included.
            response = self.http.request(method, url, **kwargs)
        except httpx2.TimeoutException:
            raise TargetError("communication", f"{label}: no response within {CALL_TIMEOUT:g} s")
        except httpx2.TransportError as exc:
            raise TargetError("communication", f"{label}: {type(exc).__name__}: {exc}")
        try:
            body = response.json() if response.content else None
        except ValueError:
            return Reply(label, response.status_code, response.headers, None, ["body is not JSON"])
        errors = response_errors(method, template, response.status_code, body)
        return Reply(label, response.status_code, response.headers, body, errors)

    def snapshot(self) -> Reply:
        """The session's stored incidents, groups and audit entries (TEST-06)."""
        return self.call("GET", "/test/report/{run_id}", None, run_id=self.session_id)
