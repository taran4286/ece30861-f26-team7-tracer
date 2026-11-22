"""An in-memory Tracer system that follows system-openapi.yaml, for testing the tester's cases.

`bugs` turns on deliberate faults (see BUGS) so tests can check that the cases catch them.
Only the routes and rules the cases use are built; the rest return 501. Due dates come from its
own holiday calendar, so the tester's expected dates are checked against an independent one.
"""

import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import holidays
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

ROLES = {"Responder", "DisclosureApprover", "Board"}
STATES = [
    "UnderAssessment", "DeterminedMaterial", "DeterminedNotMaterial", "DisclosureDelayed",
    "DisclosureRecorded", "AmendmentPending", "Closed",
]
MOVES = {
    ("DeterminedMaterial", "DisclosureDelayed"),
    ("DisclosureDelayed", "DeterminedMaterial"),
    ("DeterminedNotMaterial", "Closed"),
    ("DisclosureRecorded", "AmendmentPending"),
    ("DisclosureRecorded", "Closed"),
}
# The states a filing moves to DisclosureRecorded.
FILABLE = {"DeterminedMaterial", "DisclosureDelayed", "AmendmentPending"}
FILING_FORMS = {"8-K Item 1.05", "6-K", "8-K/A"}
SEVERITIES = {"low", "medium", "high", "critical"}
ELEMENTS = {"nature", "scope", "timing", "impact"}
DRAFT_ELEMENTS = ("nature", "scope", "timing", "impact")
UNDETERMINED = "Not yet determined"
CATEGORIES = {"operational", "financial", "legal_regulatory", "reputational", "data_compromised"}
# Drafting is allowed from these states; UnderAssessment and DeterminedNotMaterial aren't material.
DRAFTABLE = {"DeterminedMaterial", "DisclosureDelayed", "AmendmentPending"}
# Its own sensitive-detail patterns, independent of the system's.
SENSITIVE = [
    (re.compile(r"\bCVE-\d{4}-\d{4,}\b"), "CVE"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "IP address"),
    (re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])"), "IP address"),
    (re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+){2,}\b"), "hostname"),
    (re.compile(r"\bunpatched \w+"), "unpatched system"),
    (re.compile(r"\bfirewall rules?\b"), "firewall rule"),
]
ITEM_FIELDS = {
    "ticketing": ("ticket_id", "summary", "created_at"),
    "siem": ("alert_id", "rule_name", "severity", "detected_at"),
}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")
ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
DEFAULTS = {"test_now": "2026-10-05T15:00:00Z", "registrant_type": "Domestic",
            "at_risk_threshold_days": 7, "llm_base_url": "https://llm.example.edu/v1"}
# What the settings route may change: everything but registrant_type.
MUTABLE = set(DEFAULTS) - {"registrant_type"}

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
    "settings_not_built",       # the settings route returns 501
    "due_date_counts_start",    # the due date counts the start day as a business day
    "due_date_ignores_holidays",  # the due date skips weekends but not federal holidays
    "early_warning",            # warning with 2 business days left
    "overdue_on_due_date",      # overdue on the due date itself
    "stale_flags",              # flags are computed when the incident changes, not on every read
    "filing_keeps_flags",       # a filing doesn't clear warning or overdue
    "fpi_gets_8k",              # an FPI Material determination requires an 8-K and gets a due date
    "fpi_flags",                # an FPI incident gets warning and overdue from determination + 4
    "home_date_optional",       # a 6-K filing without home_disclosure_date is accepted
    "home_date_dropped",        # a filing's home_disclosure_date isn't stored
    "accession_optional",       # a filing without accession_number is accepted
    "filing_date_bumps_version",  # a filing rejected for its filing_date still bumps the version
    "filing_keeps_state",       # a filing leaves the incident in its state
    "filing_not_stored",        # a filing's 201 doesn't add it to the incident's filings
    "no_amendments",            # /transitions refuses DisclosureRecorded → AmendmentPending
    "amendment_keeps_form",     # DisclosureRecorded → AmendmentPending keeps the old required_form
    "recorded_reopens",         # /transitions allows DisclosureRecorded → DeterminedMaterial
    "amendment_closes",         # /transitions allows AmendmentPending → Closed
    "flags_not_built",          # flags are always empty
    "llm_url_fixed",            # the settings route rejects llm_base_url
    "drafting_message",         # a drafting failure's 503 says "LLM unavailable"
    "draft_drops_impact",       # drafts leave out the impact element
    "draft_keeps_missing_element",  # an element the model left out is left out of the draft
    "draft_keeps_materiality",  # a model's extra keys, materiality included, reach the draft
    "draft_moves_incident",     # a draft request adds 1 to the incident's version
    "draft_keeps_foreign_citation",  # a citation of another incident's evidence is kept
    "draft_keeps_uncited_text",  # drafted text without citations is kept
    "draft_rewrites_undetermined",  # Not yet determined comes back with a full stop
    "draft_adds_citation",      # each drafted element also cites an unknown evidence ID
    "flags_nothing",            # drafts never carry sensitive flags
    "flags_everything",         # every drafted element gets a sensitive flag
    "group_self_link",          # a group may list the same incident twice
    "group_relinks",            # incidents already in a group can join another
    "group_determination_alone",  # a determination on a member applies to it alone
    "misc_tag_accepted",        # any string is accepted as an impact category
    "tags_not_stored",          # tagging answers 200 but stores nothing
    "voluntary_ignored",        # voluntary_disclosure is accepted but never designates 8.01
    "voluntary_with_material",  # voluntary_disclosure true is accepted with Material
}


class Reject(Exception):
    def __init__(self, status: int, error: str, message: str, field: str | None = None):
        self.status, self.error, self.message, self.field = status, error, message, field


def invalid(field_name: str, message: str = "Invalid value") -> Reject:
    return Reject(400, "VALIDATION_ERROR", message, field_name)


def control_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message, "source": "system"}, status)


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@cache
def federal_holidays(year: int) -> frozenset[date]:
    return frozenset(holidays.UnitedStates(years=year, observed=True))


@dataclass
class Session:
    run: dict[str, Any]
    incidents: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)
    # Incidents with a filing for their current required_form, which clears the due-date flags.
    filed: set[str] = field(default_factory=set)

    @property
    def today(self) -> date:
        return datetime.fromisoformat(self.run["test_now"]).astimezone(UTC).date()

    @property
    def fpi(self) -> bool:
        return self.run["registrant_type"] == "FPI"

    def incident(self, incident_id: str) -> dict[str, Any]:
        stored = self.incidents.get(incident_id)
        if stored is None:
            raise Reject(404, "NOT_FOUND", "Unknown incident")
        return stored

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


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_TIME.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_timestamp(value: Any, field_name: str) -> None:
    if not valid_timestamp(value):
        raise invalid(field_name, "Expected an ISO-8601 timestamp")


def check_version(body: dict[str, Any]) -> None:
    version = body.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise invalid("version", "Expected an integer of at least 1")


def element_list(value: Any, allow_empty: bool) -> bool:
    return (isinstance(value, list) and (allow_empty or bool(value))
            and all(isinstance(e, str) and e in ELEMENTS for e in value)
            and len(set(value)) == len(value))


def setting_problem(settings: dict[str, Any]) -> str | None:
    """Why a SetupSettings or SessionSettingsUpdate value is invalid; None when all are valid."""
    for key, value in settings.items():
        if key == "test_now" and not valid_timestamp(value):
            return "test_now must be an ISO-8601 timestamp"
        if key == "registrant_type" and value not in ("Domestic", "FPI"):
            return "registrant_type must be Domestic or FPI"
        if key == "at_risk_threshold_days" and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1):
            return "at_risk_threshold_days must be an integer of at least 1"
        if key == "llm_base_url" and not (
                isinstance(value, str) and urlsplit(value).scheme and urlsplit(value).netloc):
            return "llm_base_url must be a URI"
    return None


async def control_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def sensitive_flags(text: str) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    for pattern, reason in SENSITIVE:
        for match in pattern.finditer(text):
            excerpt = match.group(0)
            if reason == "hostname" and not any(c.isalpha() for c in excerpt):
                continue
            if reason == "IP address" and ":" in excerpt:
                try:
                    ipaddress.IPv6Address(excerpt)
                except ValueError:
                    continue
            if all(f["excerpt"] != excerpt for f in flags):
                flags.append({"excerpt": excerpt, "reason": reason})
    return flags


def stub_client() -> TestClient:
    """Reaches the tester's LLM stub in-process, at any host, as the tester mounts it."""
    from tester import tester_app

    server = FastAPI()
    server.mount("/tester", tester_app)
    return TestClient(server)


class FakeSystem:
    def __init__(self, bugs: set[str] = frozenset(), elements_first: bool = False,
                 repairs_drafts: bool = False):
        """`elements_first` checks AmendmentPending's elements against the latest filing before
        the state, a 400 on `elements[i]` where there's no filing; the spec allows either order.
        `repairs_drafts` repairs a model reply that breaks the draft rules (dropping extra keys
        and bad citations, and putting Not yet determined for what's left unsupported) instead
        of answering 503; the spec allows either."""
        unknown = set(bugs) - BUGS
        assert not unknown, f"unknown bugs: {unknown}"
        self.bugs = set(bugs)
        self.elements_first = elements_first
        self.repairs_drafts = repairs_drafts
        self.sessions: dict[str, Session] = {}
        self._llm: TestClient | None = None
        self.app = self._build()

    @property
    def llm(self) -> TestClient:
        if self._llm is None:
            self._llm = stub_client()
        return self._llm

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

    # Due dates and flags (3.2.2, 3.2.3), from this system's own calendar.

    def _business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return "due_date_ignores_holidays" in self.bugs or day not in federal_holidays(day.year)

    def _due(self, start: date) -> str:
        """start + 4 business days, the start day not counted."""
        day, left = start, 4
        if "due_date_counts_start" in self.bugs and self._business_day(start):
            left -= 1
        while left:
            day += timedelta(days=1)
            left -= self._business_day(day)
        return day.isoformat()

    def _due_flags(self, session: Session, incident: dict[str, Any]) -> list[str]:
        if incident["incident_id"] in session.filed or "flags_not_built" in self.bugs:
            return []
        due = incident["due_date"]
        if due is None and "fpi_flags" in self.bugs and incident["required_form"] == "6-K":
            due = self._due(date.fromisoformat(incident["determination"]["determination_date"]))
        if due is None:
            return []
        today, due_day = session.today, date.fromisoformat(due)
        if today > due_day or (today == due_day and "overdue_on_due_date" in self.bugs):
            return ["overdue"]
        left = sum(self._business_day(today + timedelta(days=k))
                   for k in range(1, (due_day - today).days + 1))
        return ["warning"] if left <= (2 if "early_warning" in self.bugs else 1) else []

    @staticmethod
    def _check_elements(incident: dict[str, Any], elements: list[str]) -> None:
        """Each element must be among the latest filing's unavailable_elements."""
        filings = incident["filings"]
        unavailable = filings[-1]["unavailable_elements"] if filings else []
        for index, element in enumerate(elements):
            if element not in unavailable:
                raise invalid(f"elements[{index}]",
                              "Not among the latest filing's unavailable_elements")

    def _changed(self, session: Session, incident: dict[str, Any]) -> None:
        # What stale_flags serves: the flags as of the incident's last change.
        incident["flags"] = self._due_flags(session, incident)

    def _render(self, session: Session, incident: dict[str, Any]) -> dict[str, Any]:
        incident_id = incident["incident_id"]
        evidence = [e for e in session.evidence.values() if e["incident_id"] == incident_id]
        flags = incident["flags"] if "stale_flags" in self.bugs else self._due_flags(
            session, incident)
        return {**incident, "evidence": evidence, "flags": flags}

    def _build(self) -> FastAPI:
        app = FastAPI()
        everyone = set(ROLES)
        writers = {"Responder", "DisclosureApprover"}
        approver = {"DisclosureApprover"}

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
            body = await control_body(request)
            if body is None or not isinstance(body.get("session_id"), str) or not body[
                    "session_id"].strip():
                return control_error(400, "session_id is required")
            settings = body.get("setup")
            if not isinstance(settings, dict):
                return control_error(400, "setup must be an object")
            if unknown := set(settings) - set(DEFAULTS):
                return control_error(400, f"Unknown settings: {', '.join(sorted(unknown))}")
            if problem := setting_problem(settings):
                return control_error(400, problem)
            session_id = body["session_id"]
            if session_id in self.sessions:
                return control_error(409, "Duplicate session")
            run = {"run_id": session_id, **DEFAULTS, **settings, "implemented_optional": []}
            self.sessions[session_id] = Session(run)
            return {"session_id": session_id}

        @app.patch("/test/sessions/{session_id}/settings")
        async def update_settings(session_id: str, request: Request):
            if "settings_not_built" in self.bugs:
                return control_error(501, "Settings aren't built")
            session = self.sessions.get(session_id)
            if session is None:
                return control_error(404, "Unknown session")
            body = await control_body(request)
            if body is None:
                return control_error(400, "Expected a JSON object")
            if "registrant_type" in body:
                return control_error(400, "registrant_type is fixed for a session")
            if unknown := set(body) - MUTABLE:
                return control_error(400, f"Unknown settings: {', '.join(sorted(unknown))}")
            if "llm_base_url" in body and "llm_url_fixed" in self.bugs:
                return control_error(400, "llm_base_url can't be changed")
            if problem := setting_problem(body):
                return control_error(400, problem)
            session.run |= body
            return session.run

        @app.get("/test/report/{run_id}")
        def snapshot(run_id: str):
            session = self.sessions.get(run_id)
            if session is None:
                return control_error(404, "Unknown session")
            groups = [{"group_id": g, "incident_ids": ids} for g, ids in session.groups.items()]
            return {"run": session.run, "groups": groups, "audit": session.audit,
                    "incidents": [self._render(session, i) for i in session.incidents.values()]}

        @app.get("/test/report")
        def coverage(session_id: str):
            if session_id not in self.sessions:
                return control_error(404, "Unknown session")
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
            return self._render(session, incident)

        @app.get("/incidents/{incident_id}")
        def get_incident(incident_id: str, request: Request):
            session, user = self._identity(request, everyone)
            incident = session.incident(incident_id)
            if "audited_reads" in self.bugs:
                session.write_audit(user, "transition", incident_id, None, {"read": True})
            return self._render(session, incident)

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
            group = session.groups.get(incident["group_id"], [incident_id])
            if "group_determination_alone" in self.bugs:
                group = [incident_id]
            day = parse_date(body.get("determination_date"), "determination_date")
            discovered = max(date.fromisoformat(session.incidents[m]["discovery_date"])
                             for m in group)
            if not discovered <= day <= session.today:
                raise invalid("determination_date", "Must be between discovery and today")
            rationale = body.get("rationale")
            if not isinstance(rationale, str) or not rationale:
                raise invalid("rationale")
            refs = body.get("evidence_refs")
            if not isinstance(refs, list) or not refs or len(set(map(str, refs))) != len(refs):
                raise invalid("evidence_refs", "Expected unique evidence IDs")
            if any(session.evidence.get(ref, {}).get("incident_id") not in group for ref in refs):
                raise invalid("evidence_refs", "Every ref must be evidence of this incident or "
                              "its group")
            material = body["decision"] == "Material"
            voluntary = body.get("voluntary_disclosure", False)
            if not isinstance(voluntary, bool):
                raise invalid("voluntary_disclosure", "Expected a boolean")
            if voluntary and material and "voluntary_with_material" not in self.bugs:
                raise invalid("voluntary_disclosure", "Valid only with NotMaterial")
            if "voluntary_ignored" in self.bugs:
                voluntary = False
            check_version(body)
            if any(session.incidents[m]["state"] != "UnderAssessment" for m in group):
                raise Reject(409, "INVALID_TRANSITION", "Only from UnderAssessment")
            fpi = session.fpi and "fpi_gets_8k" not in self.bugs
            form = ("6-K" if fpi else "8-K Item 1.05") if material else (
                "8-K Item 8.01" if voluntary else None)
            for member in (session.incidents[m] for m in group):
                before = {"state": member["state"], "determination": None,
                          "version": member["version"]}
                member.update(
                    state="DeterminedMaterial" if material else "DeterminedNotMaterial",
                    required_form=form,
                    due_date=self._due(day) if form == "8-K Item 1.05" else None,
                    version=member["version"] + 1,
                    determination={"decision": body["decision"],
                                   "determination_date": day.isoformat(),
                                   "rationale": rationale, "evidence_refs": refs,
                                   "voluntary_disclosure": voluntary, "recorded_by": user,
                                   "recorded_at": now()},
                )
                self._changed(session, member)
                session.write_audit(user, "record_determination", member["incident_id"], before,
                                    {"state": member["state"], "version": member["version"]})
            return self._render(session, incident)

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
                if not element_list(elements, allow_empty=False):
                    raise invalid("elements")
            moves = set(MOVES)
            if "dnm_to_dm" in self.bugs:
                moves.add(("DeterminedNotMaterial", "DeterminedMaterial"))
            if "transitions_determine" in self.bugs:
                moves.add(("UnderAssessment", "DeterminedMaterial"))
            if "recorded_reopens" in self.bugs:
                moves.add(("DisclosureRecorded", "DeterminedMaterial"))
            if "amendment_closes" in self.bugs:
                moves.add(("AmendmentPending", "Closed"))
            if "no_amendments" in self.bugs:
                moves.discard(("DisclosureRecorded", "AmendmentPending"))
            if to == "AmendmentPending" and self.elements_first:
                self._check_elements(incident, elements)
            if (incident["state"], to) not in moves:
                raise Reject(409, "INVALID_TRANSITION", f"{incident['state']} → {to} not allowed")
            if to == "AmendmentPending":
                self._check_elements(incident, elements)
            before = {"state": incident["state"], "version": incident["version"]}
            incident.update(state=to, version=incident["version"] + 1)
            if to == "AmendmentPending":
                if "amendment_keeps_form" not in self.bugs:
                    incident["required_form"] = "6-K" if session.fpi else "8-K/A"
                incident["due_date"] = None if session.fpi else self._due(available)
                session.filed.discard(incident_id)
            if "transitions_determine" in self.bugs and before["state"] == "UnderAssessment":
                incident["determination"] = {
                    "decision": "Material", "determination_date": "2026-10-05", "rationale": "x",
                    "evidence_refs": [], "recorded_by": user, "recorded_at": now(),
                }
            self._changed(session, incident)
            session.write_audit(user, "transition", incident_id, before,
                                {"state": to, "version": incident["version"]})
            return self._render(session, incident)

        @app.post("/incidents/{incident_id}/filings", status_code=201)
        async def record_filing(incident_id: str, request: Request):
            session, user = self._identity(request, approver)
            incident = session.incident(incident_id)
            body = await json_body(request)
            form = body.get("form_type")
            if form not in FILING_FORMS:
                raise invalid("form_type")
            try:
                filed_on = parse_date(body.get("filing_date"), "filing_date")
            except Reject:
                if "filing_date_bumps_version" in self.bugs:
                    incident["version"] += 1
                raise
            determination = incident["determination"]
            if determination and filed_on < date.fromisoformat(
                    determination["determination_date"]):
                raise invalid("filing_date", "Before the determination")
            if filed_on > session.today:
                raise invalid("filing_date", "After today")
            accession = body.get("accession_number")
            if accession is None and "accession_optional" in self.bugs:
                accession = ""
            elif not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
                raise invalid("accession_number")
            unavailable = body.get("unavailable_elements", [])
            if not element_list(unavailable, allow_empty=True):
                raise invalid("unavailable_elements")
            home = body.get("home_disclosure_date")
            if home is not None:
                parse_date(home, "home_disclosure_date")
            elif form == "6-K" and "home_date_optional" not in self.bugs:
                raise invalid("home_disclosure_date", "Required for a 6-K")
            check_version(body)
            if incident["required_form"] is not None and form != incident["required_form"]:
                raise invalid("form_type", f"Must be the required {incident['required_form']}")
            if incident["state"] not in FILABLE:
                raise Reject(409, "INVALID_TRANSITION", f"No filing from {incident['state']}")
            before = {"state": incident["state"], "version": incident["version"]}
            if "filing_not_stored" not in self.bugs:
                incident["filings"].append({
                    "form_type": form, "filing_date": filed_on.isoformat(),
                    "accession_number": accession, "unavailable_elements": unavailable,
                    "home_disclosure_date": None if "home_date_dropped" in self.bugs else home,
                    "recorded_by": user, "recorded_at": now(),
                })
            incident["version"] += 1
            if "filing_keeps_state" not in self.bugs:
                incident["state"] = "DisclosureRecorded"
            if "filing_keeps_flags" not in self.bugs:
                session.filed.add(incident_id)
            self._changed(session, incident)
            session.write_audit(user, "record_filing", incident_id, before,
                                {"state": incident["state"], "version": incident["version"],
                                 "accession_number": accession})
            return self._render(session, incident)

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
        async def tag_evidence(incident_id: str, evidence_id: str, request: Request):
            session, user = self._identity(request, writers)
            session.incident(incident_id)
            item = session.evidence.get(evidence_id)
            if item is None or item["incident_id"] != incident_id:
                raise Reject(404, "NOT_FOUND", "Unknown evidence")
            body = await json_body(request)
            categories = body.get("impact_categories")
            allowed = (lambda c: isinstance(c, str)) if "misc_tag_accepted" in self.bugs else (
                lambda c: c in CATEGORIES)
            if not isinstance(categories, list) or len(set(map(str, categories))) != len(
                    categories) or not all(map(allowed, categories)):
                raise invalid("impact_categories", "Expected unique impact categories")
            before = list(item["impact_categories"])
            if "tags_not_stored" not in self.bugs:
                item["impact_categories"] = list(categories)
            session.write_audit(user, "tag_evidence", incident_id, {"impact_categories": before},
                                {"impact_categories": categories})
            return {**item, "impact_categories": list(categories)}

        @app.post("/groups", status_code=201)
        async def create_group(request: Request):
            session, user = self._identity(request, writers)
            body = await json_body(request)
            ids = body.get("incident_ids")
            if not isinstance(ids, list) or len(ids) < 2 or not all(
                    isinstance(i, str) and i for i in ids):
                raise invalid("incident_ids", "Expected at least two incident IDs")
            if len(set(ids)) != len(ids) and "group_self_link" not in self.bugs:
                raise invalid("incident_ids", "An incident can't be linked to itself")
            for index, incident_id in enumerate(ids):
                if incident_id not in session.incidents:
                    raise Reject(404, "NOT_FOUND", "Unknown incident", f"incident_ids[{index}]")
            members = list(dict.fromkeys(ids))
            for incident_id in members:
                incident = session.incidents[incident_id]
                grouped = incident["group_id"] is not None and "group_relinks" not in self.bugs
                if grouped or incident["state"] != "UnderAssessment":
                    raise Reject(409, "CONFLICT", "Members must be ungrouped and UnderAssessment")
            group_id = new_id("grp")
            session.groups[group_id] = ids
            for incident_id in members:
                incident = session.incidents[incident_id]
                before = {"group_id": incident["group_id"], "version": incident["version"]}
                incident.update(group_id=group_id, version=incident["version"] + 1)
                session.write_audit(user, "create_group", incident_id, before,
                                    {"group_id": group_id, "version": incident["version"]})
            return {"group_id": group_id, "incident_ids": ids}

        @app.post("/incidents/{incident_id}/drafts", status_code=201)
        def request_draft(incident_id: str, request: Request):
            session, _ = self._identity(request, approver)
            incident = session.incident(incident_id)
            if incident["state"] in ("UnderAssessment", "DeterminedNotMaterial"):
                raise Reject(409, "NOT_MATERIAL", "Drafts are only for material incidents")
            if incident["state"] not in DRAFTABLE:
                raise Reject(409, "INVALID_TRANSITION", f"No draft from {incident['state']}")
            evidence = [e for e, item in session.evidence.items()
                        if item["incident_id"] == incident_id]
            extras: dict[str, Any] = {}
            if evidence:
                elements, extras = self._drafted(session, evidence)
            else:
                elements = {name: self._undetermined() for name in DRAFT_ELEMENTS}
            if "draft_drops_impact" in self.bugs:
                del elements["impact"]
            if "draft_moves_incident" in self.bugs:
                incident["version"] += 1
            body = {"draft_id": new_id("drf"), "incident_id": incident_id,
                    "form_type": incident["required_form"], "elements": elements,
                    "advisory": True, "status": "Proposed", "accepted_by": None,
                    "accepted_at": None}
            return {**body, **extras}

        @app.post("/drafts/{draft_id}/accept")
        def accept_draft(request: Request):
            self._identity(request, approver)
            raise Reject(404, "NOT_FOUND", "Unknown draft")

        return app

    def _undetermined(self) -> dict[str, Any]:
        text = UNDETERMINED + ("." if "draft_rewrites_undetermined" in self.bugs else "")
        return {"text": text, "citations": [], "sensitive_flags": []}

    def _drafted(self, session: Session, evidence: list[str]) -> tuple[dict, dict]:
        """The elements from the session's LLM, checked against the draft rules; 503 for a
        reply that breaks them, unless repairs_drafts mends it."""
        message = "LLM unavailable" if "drafting_message" in self.bugs else "Drafting unavailable"
        unavailable = Reject(503, "DRAFTING_UNAVAILABLE", message)
        try:
            response = self.llm.post(f"{session.run['llm_base_url'].rstrip('/')}/chat/completions",
                                     json={"messages": [{"role": "user", "content": "Draft"}]})
            content = json.loads(response.json()["choices"][0]["message"]["content"])
        except Exception:
            raise unavailable
        if not response.is_success or not isinstance(content, dict):
            raise unavailable
        extras = {k: v for k, v in content.items() if k not in ELEMENTS}
        if extras and not self.repairs_drafts and "draft_keeps_materiality" not in self.bugs:
            raise unavailable
        elements = {}
        for name in DRAFT_ELEMENTS:
            if name not in content and "draft_keeps_missing_element" in self.bugs:
                continue
            try:
                elements[name] = self._checked(session, evidence, content.get(name))
            except ValueError:
                if not self.repairs_drafts:
                    raise unavailable
                elements[name] = self._undetermined()
        return elements, (extras if "draft_keeps_materiality" in self.bugs else {})

    def _checked(self, session: Session, evidence: list[str], item: Any) -> dict[str, Any]:
        """One element, or ValueError when it breaks the rules and can't be kept."""
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(
                item.get("citations"), list):
            raise ValueError("not an element")
        text, citations = item["text"], item["citations"]
        if text == UNDETERMINED:
            if citations and not self.repairs_drafts:
                raise ValueError("citations on an undetermined element")
            return self._undetermined()
        known = (lambda c: c in session.evidence) if (
            "draft_keeps_foreign_citation" in self.bugs) else (lambda c: c in evidence)
        kept = [c for c in citations if known(c)]
        if len(kept) != len(citations) and not self.repairs_drafts:
            raise ValueError("a citation outside the incident's evidence")
        if not kept and "draft_keeps_uncited_text" not in self.bugs:
            raise ValueError("text without citations")
        if "flags_nothing" in self.bugs:
            flags = []
        elif "flags_everything" in self.bugs:
            flags = [{"excerpt": text, "reason": "detail"}]
        else:
            flags = sensitive_flags(text)
        if "draft_adds_citation" in self.bugs:
            kept = [*kept, "ev-unknown"]
        return {"text": text, "citations": kept, "sensitive_flags": flags}

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
