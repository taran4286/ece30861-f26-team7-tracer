"""HTTP access to one target system for one test session."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

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


class Stopped(Exception):
    """The run passed its deadline, so this call was never sent."""


@dataclass
class Reply:
    """One response. sent_at and received_at are the tester's own UTC clock just before the
    request went out and just after the response arrived, so the target handled it between them."""

    label: str
    status: int
    headers: httpx2.Headers
    body: Any
    sent_at: datetime
    received_at: datetime
    schema_errors: list[str] = field(default_factory=list)

    def detail(self) -> str:
        """The error code and message of an Error body, for test messages."""
        if isinstance(self.body, dict) and "error" in self.body:
            return f" {self.body.get('error')}: {self.body.get('message')}"
        return ""


class Target:
    """Sends project and test-control calls to a system base URL as one session.

    `http` is an httpx2 client whose base URL is the system base URL. Every reply is
    checked against the spec's schema for its operation and status. `deadline` is a
    `time.monotonic()` value after which `call` sends nothing; `called` records each
    (method, template) sent, for component coverage.
    """

    def __init__(self, http: httpx2.Client, session_id: str, deadline: float | None = None):
        self.http = http
        self.session_id = session_id
        self.deadline = deadline
        self.stopped = False
        self.called: set[tuple[str, str]] = set()

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

        Raises Stopped once the deadline has passed, and TargetError("communication") when no
        response arrives.
        """
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.stopped = True
            raise Stopped(f"{method} {template} wasn't sent: the run reached its time limit")
        return self._send(method, template, actor, json=json, path=path)

    def coverage(self) -> Reply:
        """The session's code coverage report (TEST-02).

        Not held to the deadline: the run asks for it after its last case, aborted or not.
        """
        return self._send("GET", "/test/report", None, query={"session_id": self.session_id})

    def snapshot(self) -> Reply:
        """The session's stored incidents, groups and audit entries (TEST-06)."""
        return self.call("GET", "/test/report/{run_id}", None, run_id=self.session_id)

    def _send(
        self,
        method: str,
        template: str,
        actor: Actor | None,
        *,
        json: Any = NO_BODY,
        path: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> Reply:
        label = f"{method} {template}"
        self.called.add((method, template))
        url = template.format(**{k: quote(str(v), safe="") for k, v in (path or {}).items()})
        if query:
            url += f"?{urlencode(query)}"
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
        sent_at = datetime.now(UTC)
        try:
            # httpx2 appends a relative URL to the base URL's path, /system included.
            response = self.http.request(method, url, **kwargs)
        except httpx2.TimeoutException:
            raise TargetError("communication", f"{label}: no response within {CALL_TIMEOUT:g} s")
        except httpx2.TransportError as exc:
            raise TargetError("communication", f"{label}: {type(exc).__name__}: {exc}")
        received_at = datetime.now(UTC)
        reply = Reply(label, response.status_code, response.headers, None, sent_at, received_at)
        try:
            reply.body = response.json() if response.content else None
        except ValueError:
            reply.schema_errors = ["body is not JSON"]
            return reply
        reply.schema_errors = response_errors(method, template, response.status_code, reply.body)
        return reply
