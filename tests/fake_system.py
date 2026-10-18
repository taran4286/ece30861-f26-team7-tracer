"""An in-memory Tracer system that follows system-openapi.yaml, for testing the tester's cases.

`bugs` turns on deliberate faults (see BUGS) so tests can check that the cases catch them.
Only the routes and rules the cases use are built; the rest return 501.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import formatdate
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ROLES = {"Responder", "DisclosureApprover", "Board"}
STATES = [
    "UnderAssessment", "DeterminedMaterial", "DeterminedNotMaterial", "DisclosureDelayed",
    "DisclosureRecorded", "AmendmentPending", "Closed",
]
MOVES = {
    ("DeterminedMaterial", "DisclosureDelayed"),
    ("DisclosureDelayed", "DeterminedMaterial"),
    ("DeterminedNotMaterial", "Closed"),
}
SEVERITIES = {"low", "medium", "high", "critical"}
ELEMENTS = {"nature", "scope", "timing", "impact"}
ITEM_FIELDS = {
    "ticketing": ("ticket_id", "summary", "created_at"),
    "siem": ("alert_id", "rule_name", "severity", "detected_at"),
}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")

BUGS = {
    "responder_determines",     # a Responder can record a determination
    "dnm_to_dm",                # /transitions allows DeterminedNotMaterial → DeterminedMaterial
    "transitions_determine",    # /transitions moves UnderAssessment to DeterminedMaterial
    "partial_import",           # a rejected batch keeps the items before the bad one
    "future_discovery",         # a discovery_date after today is accepted
    "imported_at_test_now",     # imported_at is the session's test_now, not server time
    "audited_reads",            # GET /incidents/{id} writes an audit entry
    "audit_delete",             # DELETE on the audit trail clears it
    "board_import_crashes",     # an import as Board returns 500
}


class Reject(Exception):
    def __init__(self, status: int, error: str, message: str, field: str | None = None):
        self.status, self.error, self.message, self.field = status, error, message, field


def invalid(field_name: str, message: str = "Invalid value") -> Reject:
    return Reject(400, "VALIDATION_ERROR", message, field_name)


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class Session:
    run: dict[str, Any]
    today: date
    incidents: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def incident(self, incident_id: str) -> dict[str, Any]:
        stored = self.incidents.get(incident_id)
        if stored is None:
            raise Reject(404, "NOT_FOUND", "Unknown incident")
        return stored

    def render(self, incident: dict[str, Any]) -> dict[str, Any]:
        incident_id = incident["incident_id"]
        evidence = [e for e in self.evidence.values() if e["incident_id"] == incident_id]
        return {**incident, "evidence": evidence}

    def write_audit(self, user: str, action: str, target: str, before: Any, after: Any) -> None:
        self.audit.append({"entry_id": new_id("aud"), "user_id": user, "timestamp": now(),
                           "action": action, "target_id": target, "before": before,
                           "after": after})


def parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str) or not DATE.fullmatch(value):
        raise invalid(field_name, "Expected YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid(field_name, "Not a real date")


def check_timestamp(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not DATE_TIME.fullmatch(value):
        raise invalid(field_name, "Expected an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise invalid(field_name, "Not a real timestamp")


def check_version(body: dict[str, Any]) -> None:
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise invalid("version", "Expected an integer of at least 1")


class FakeSystem:
    def __init__(self, bugs: set[str] = frozenset()):
        unknown = set(bugs) - BUGS
        assert not unknown, f"unknown bugs: {unknown}"
        self.bugs = set(bugs)
        self.sessions: dict[str, Session] = {}
        self.app = self._build()

    def _identity(self, request: Request, allowed: set[str]) -> tuple[Session, str]:
        user = request.headers.get("X-User-Id")
        role = request.headers.get("X-User-Role")
        run = request.headers.get("X-Test-Run")
        if not user:
            raise invalid("X-User-Id", "Missing X-User-Id")
        if role not in ROLES:
            raise invalid("X-User-Role", "Missing or unknown X-User-Role")
        if not run:
            raise invalid("X-Test-Run", "Missing X-Test-Run")
        if role not in allowed:
            raise Reject(403, "FORBIDDEN_ROLE", f"{role} can't do this")
        session = self.sessions.get(run)
        if session is None:
            raise Reject(404, "NOT_FOUND", "Unknown run")
        return session, user

    def _build(self) -> FastAPI:
        app = FastAPI()
        everyone = set(ROLES)
        writers = {"Responder", "DisclosureApprover"}
        approver = {"DisclosureApprover"}

        @app.middleware("http")
        async def server_date(request: Request, call_next):
            response = await call_next(request)
            response.headers["Date"] = formatdate(usegmt=True)
            return response

        @app.exception_handler(Reject)
        async def rejected(request: Request, exc: Reject) -> JSONResponse:
            body = {"error": exc.error, "message": exc.message}
            if exc.field is not None:
                body["field"] = exc.field
            return JSONResponse(body, status_code=exc.status)

        async def json_body(request: Request) -> dict[str, Any]:
            try:
                body = await request.json()
            except ValueError:
                raise invalid("body", "Expected a JSON object")
            if not isinstance(body, dict):
                raise invalid("body", "Expected a JSON object")
            return body

        @app.post("/test/setup", status_code=201)
        async def setup(request: Request):
            body = await request.json()
            session_id = body.get("session_id")
            if session_id in self.sessions:
                return JSONResponse({"message": "Duplicate session", "source": "system"}, 409)
            test_now = body.get("setup", {}).get("test_now", "2026-10-05T15:00:00Z")
            run = {"run_id": session_id, "test_now": test_now, "registrant_type": "Domestic",
                   "at_risk_threshold_days": 7, "llm_base_url": "https://llm.example.edu/v1",
                   "implemented_optional": []}
            self.sessions[session_id] = Session(run, datetime.fromisoformat(test_now).date())
            return {"session_id": session_id}

        @app.get("/test/report/{run_id}")
        def snapshot(run_id: str):
            session = self.sessions.get(run_id)
            if session is None:
                return JSONResponse({"message": "Unknown session", "source": "system"}, 404)
            return {"run": session.run, "groups": [], "audit": session.audit,
                    "incidents": [session.render(i) for i in session.incidents.values()]}

        @app.get("/test/report")
        def coverage(session_id: str):
            if session_id not in self.sessions:
                return JSONResponse({"message": "Unknown session", "source": "system"}, 404)
            unmeasured = {"covered": None, "total": None,
                          "basis": "The fake system doesn't measure code coverage"}
            return {"session_id": session_id, "coverage": {"code": unmeasured}}

        @app.post("/incidents", status_code=201)
        async def create_incident(request: Request):
            session, user = self._identity(request, writers)
            body = await json_body(request)
            title = body.get("title")
            if not isinstance(title, str) or not title:
                raise invalid("title")
            discovery = parse_date(body.get("discovery_date"), "discovery_date")
            if discovery > session.today and "future_discovery" not in self.bugs:
                raise invalid("discovery_date", "Discovery date is after today")
            incident = {"incident_id": new_id("inc"), "title": title,
                        "discovery_date": discovery.isoformat(), "state": "UnderAssessment",
                        "determination": None, "required_form": None, "due_date": None,
                        "flags": [], "filings": [], "group_id": None, "version": 1}
            session.incidents[incident["incident_id"]] = incident
            session.write_audit(user, "create_incident", incident["incident_id"], None,
                                {"state": "UnderAssessment", "title": title,
                                 "discovery_date": incident["discovery_date"], "version": 1})
            return session.render(incident)

        @app.get("/incidents/{incident_id}")
        def get_incident(incident_id: str, request: Request):
            session, user = self._identity(request, everyone)
            incident = session.incident(incident_id)
            if "audited_reads" in self.bugs:
                session.write_audit(user, "transition", incident_id, None, {"read": True})
            return session.render(incident)

        @app.api_route("/incidents/{incident_id}", methods=["PUT", "DELETE"])
        @app.api_route("/incidents/{incident_id}/evidence/{evidence_id}", methods=["DELETE"])
        def not_allowed(request: Request):
            self._identity(request, approver)
            raise Reject(405, "METHOD_NOT_ALLOWED", "Records are never edited or deleted")

        @app.post("/incidents/{incident_id}/evidence/import", status_code=201)
        async def import_evidence(incident_id: str, request: Request):
            role = request.headers.get("X-User-Role")
            if role == "Board" and "board_import_crashes" in self.bugs:
                return JSONResponse({"error": "INTERNAL", "message": "boom"}, 500)
            session, user = self._identity(request, writers)
            session.incident(incident_id)
            body = await json_body(request)
            source = body.get("source")
            if source not in ITEM_FIELDS:
                raise invalid("source")
            items = body.get("items")
            if not isinstance(items, list) or not items:
                raise invalid("items", "Expected at least one item")
            stamp = session.run["test_now"] if "imported_at_test_now" in self.bugs else now()
            evidence = []
            for n, item in enumerate(items):
                try:
                    self._check_item(source, item, f"items[{n}]")
                except Reject:
                    if "partial_import" in self.bugs:
                        session.evidence |= {e["evidence_id"]: e for e in evidence}
                    raise
                ref_key, summary_key, *_, time_key = ITEM_FIELDS[source]
                evidence.append({
                    "evidence_id": new_id("ev"), "incident_id": incident_id, "source": source,
                    "source_ref": item[ref_key], "summary": item[summary_key],
                    "observed_at": item[time_key], "imported_by": user, "imported_at": stamp,
                    "impact_categories": [],
                })
            session.evidence |= {e["evidence_id"]: e for e in evidence}
            evidence_ids = [e["evidence_id"] for e in evidence]
            session.write_audit(user, "import_evidence", incident_id, None,
                                {"source": source, "evidence_ids": evidence_ids})
            return {"evidence": evidence}

        @app.post("/incidents/{incident_id}/determination")
        async def record_determination(incident_id: str, request: Request):
            allowed = writers if "responder_determines" in self.bugs else approver
            session, user = self._identity(request, allowed)
            incident = session.incident(incident_id)
            body = await json_body(request)
            if body.get("decision") not in ("Material", "NotMaterial"):
                raise invalid("decision")
            day = parse_date(body.get("determination_date"), "determination_date")
            if not date.fromisoformat(incident["discovery_date"]) <= day <= session.today:
                raise invalid("determination_date", "Must be between discovery and today")
            rationale = body.get("rationale")
            if not isinstance(rationale, str) or not rationale:
                raise invalid("rationale")
            refs = body.get("evidence_refs")
            if not isinstance(refs, list) or not refs or len(set(map(str, refs))) != len(refs):
                raise invalid("evidence_refs", "Expected unique evidence IDs")
            if any(session.evidence.get(ref, {}).get("incident_id") != incident_id for ref in refs):
                raise invalid("evidence_refs", "Every ref must be this incident's evidence")
            check_version(body)
            if incident["state"] != "UnderAssessment":
                raise Reject(409, "INVALID_TRANSITION", "Only from UnderAssessment")
            material = body["decision"] == "Material"
            before = {"state": incident["state"], "determination": None,
                      "version": incident["version"]}
            incident.update(
                state="DeterminedMaterial" if material else "DeterminedNotMaterial",
                required_form="8-K Item 1.05" if material else None,
                version=incident["version"] + 1,
                determination={"decision": body["decision"], "determination_date": day.isoformat(),
                               "rationale": rationale, "evidence_refs": refs,
                               "voluntary_disclosure": False, "recorded_by": user,
                               "recorded_at": now()},
            )
            session.write_audit(user, "record_determination", incident_id, before,
                                {"state": incident["state"], "version": incident["version"]})
            return session.render(incident)

        @app.post("/incidents/{incident_id}/transitions")
        async def transition(incident_id: str, request: Request):
            session, user = self._identity(request, approver)
            incident = session.incident(incident_id)
            body = await json_body(request)
            to = body.get("to")
            if to not in STATES:
                raise invalid("to")
            check_version(body)
            if to == "AmendmentPending":
                available = parse_date(body.get("info_available_date"), "info_available_date")
                if available > session.today:
                    raise invalid("info_available_date", "After today")
                elements = body.get("elements")
                if (not isinstance(elements, list) or not elements
                        or not set(elements) <= ELEMENTS or len(set(elements)) != len(elements)):
                    raise invalid("elements")
            moves = set(MOVES)
            if "dnm_to_dm" in self.bugs:
                moves.add(("DeterminedNotMaterial", "DeterminedMaterial"))
            if "transitions_determine" in self.bugs:
                moves.add(("UnderAssessment", "DeterminedMaterial"))
            if (incident["state"], to) not in moves:
                raise Reject(409, "INVALID_TRANSITION", f"{incident['state']} → {to} not allowed")
            before = {"state": incident["state"], "version": incident["version"]}
            incident.update(state=to, version=incident["version"] + 1)
            if "transitions_determine" in self.bugs and before["state"] == "UnderAssessment":
                incident["determination"] = {
                    "decision": "Material", "determination_date": "2026-10-05", "rationale": "x",
                    "evidence_refs": [], "recorded_by": user, "recorded_at": now(),
                }
            session.write_audit(user, "transition", incident_id, before,
                                {"state": to, "version": incident["version"]})
            return session.render(incident)

        @app.get("/incidents/{incident_id}/audit")
        def get_audit(incident_id: str, request: Request):
            session, _ = self._identity(request, everyone)
            session.incident(incident_id)
            return {"entries": [e for e in session.audit if e["target_id"] == incident_id]}

        @app.api_route("/incidents/{incident_id}/audit", methods=["PUT", "PATCH", "DELETE"])
        def edit_audit(incident_id: str, request: Request):
            session, _ = self._identity(request, approver)
            if request.method == "DELETE" and "audit_delete" in self.bugs:
                session.audit = [e for e in session.audit if e["target_id"] != incident_id]
                return {"entries": []}
            raise Reject(405, "METHOD_NOT_ALLOWED", "The audit trail is append-only")

        @app.patch("/incidents/{incident_id}/evidence/{evidence_id}")
        def tag_evidence(request: Request):
            self._identity(request, writers)
            raise Reject(501, "NOT_IMPLEMENTED", "REQ-05 isn't built")

        @app.post("/groups")
        def create_group(request: Request):
            self._identity(request, writers)
            raise Reject(501, "NOT_IMPLEMENTED", "REQ-04 isn't built")

        @app.post("/incidents/{incident_id}/filings")
        @app.post("/incidents/{incident_id}/drafts")
        @app.post("/drafts/{draft_id}/accept")
        def phase_4(request: Request):
            self._identity(request, approver)
            raise Reject(501, "NOT_IMPLEMENTED", "Phase 4")

        return app

    @staticmethod
    def _check_item(source: str, item: Any, path: str) -> None:
        if not isinstance(item, dict):
            raise invalid(path, "Expected an object")
        for key in ITEM_FIELDS[source]:
            if key not in item:
                raise invalid(f"{path}.{key}", "Missing field")
            if key in ("created_at", "detected_at"):
                check_timestamp(item[key], f"{path}.{key}")
            elif key == "severity":
                if item[key] not in SEVERITIES:
                    raise invalid(f"{path}.{key}")
            elif not isinstance(item[key], str) or not item[key]:
                raise invalid(f"{path}.{key}")
