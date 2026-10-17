"""The cross-team test cases, in run order: AC-02, 03, 07, 08, 20, 06, 09, 23, then 01.

Each criterion has normal cases and edge cases (boundaries, bad input, wrong roles, unknown
IDs). Pass rules are the course's acceptance criteria; expected statuses, error codes and
fields come from system-openapi.yaml.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import cache
from pathlib import Path
from typing import Any

from tester.client import NO_BODY, Actor, Reply, Target, TargetError
from tester.suite import Case, Fail, Run, Skip, expect, run_cases

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# "Today" is the date of the session's test_now. The tester's setup sends {}, so test_now is
# the spec's default, 2026-10-05T15:00:00Z.
TODAY = date(2026, 10, 5)
TOMORROW = TODAY + timedelta(days=1)
# AC-02's tolerance against server time, used for every server timestamp.
CLOCK_TOLERANCE = timedelta(seconds=5)

RESPONDER = Actor("Responder", "tester-responder")
RESPONDER_2 = Actor("Responder", "tester-responder-2")
APPROVER = Actor("DisclosureApprover", "tester-approver")
BOARD = Actor("Board", "tester-board")
ACTORS = {"Responder": RESPONDER, "DisclosureApprover": APPROVER, "Board": BOARD}

UA, DM, DNM = "UnderAssessment", "DeterminedMaterial", "DeterminedNotMaterial"
DELAYED, RECORDED = "DisclosureDelayed", "DisclosureRecorded"
AMENDMENT, CLOSED = "AmendmentPending", "Closed"
STATES = [UA, DM, DNM, DELAYED, RECORDED, AMENDMENT, CLOSED]
# Test IDs shorten these three, as in AC-06-DM-to-Closed.
SHORT = {UA: "UA", DM: "DM", DNM: "DNM"}

# The spec's state table (3.2.1), by the route that performs each move.
DETERMINATION_MOVES = {(UA, DM), (UA, DNM)}
TRANSITION_MOVES = {(DM, DELAYED), (DELAYED, DM), (DNM, CLOSED)}
FILING_MOVES = {(DM, RECORDED), (DELAYED, RECORDED)}

INCIDENTS = "/incidents"
INCIDENT = "/incidents/{incident_id}"
IMPORT = "/incidents/{incident_id}/evidence/import"
EVIDENCE = "/incidents/{incident_id}/evidence/{evidence_id}"
DETERMINATION = "/incidents/{incident_id}/determination"
TRANSITIONS = "/incidents/{incident_id}/transitions"
FILINGS = "/incidents/{incident_id}/filings"
DRAFTS = "/incidents/{incident_id}/drafts"
ACCEPT = "/drafts/{draft_id}/accept"
AUDIT = "/incidents/{incident_id}/audit"
GROUPS = "/groups"

UNKNOWN_ID = "tester-unknown-id"
IMPOSSIBLE_DATE = "2026-02-30"


@cache
def _fixture(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def incident_body(n: int) -> dict[str, Any]:
    return dict(_fixture("incidents")[n % 10])


def ticket(n: int) -> dict[str, Any]:
    return dict(_fixture("ticketing")[n % 20])


def alert(n: int) -> dict[str, Any]:
    return dict(_fixture("siem")[n % 20])


@dataclass
class Tracked:
    """An incident the tester created, with the state and version the target last reported."""

    id: str
    discovery: date
    state: str
    version: int
    evidence: list[str]

    def update(self, incident: dict[str, Any]) -> None:
        self.state = incident["state"]
        self.version = incident["version"]


# Calls every case builds on. Each raises Fail or TargetError when the target doesn't do it.


def create(run: Run, n: int, actor: Actor = RESPONDER) -> Tracked:
    body = incident_body(n)
    incident = expect(run.call("POST", INCIDENTS, actor, json=body), 201)
    return Tracked(
        incident["incident_id"], date.fromisoformat(body["discovery_date"]),
        incident["state"], incident["version"], [],
    )


def import_items(
    run: Run, inc: Tracked, source: str, items: list[dict[str, Any]], actor: Actor = RESPONDER
) -> list[dict[str, Any]]:
    reply = run.call("POST", IMPORT, actor, json={"source": source, "items": items},
                     incident_id=inc.id)
    evidence = expect(reply, 201)["evidence"]
    if len(evidence) != len(items):
        raise Fail(f"{reply.label}: sent {len(items)} items, got {len(evidence)} back")
    inc.evidence += [item["evidence_id"] for item in evidence]
    return evidence


def get(run: Run, incident_id: str, actor: Actor = APPROVER) -> dict[str, Any]:
    return expect(run.call("GET", INCIDENT, actor, incident_id=incident_id), 200)


def determination(
    inc: Tracked, decision: str = "Material", day: date | None = None,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """A valid DeterminationRequest body, dated the day after discovery unless `day` is given."""
    day = day or min(inc.discovery + timedelta(days=1), TODAY)
    return {
        "decision": decision,
        "determination_date": day.isoformat(),
        "rationale": "Assessed by the tester from the imported evidence.",
        "evidence_refs": inc.evidence[:1] if refs is None else refs,
        "version": inc.version,
    }


def determine(run: Run, inc: Tracked, decision: str = "Material", **kwargs: Any) -> dict[str, Any]:
    reply = run.call("POST", DETERMINATION, APPROVER, json=determination(inc, decision, **kwargs),
                     incident_id=inc.id)
    incident = expect(reply, 200)
    inc.update(incident)
    return incident


def transition_body(inc: Tracked, to: str) -> dict[str, Any]:
    body: dict[str, Any] = {"to": to, "version": inc.version}
    if to == AMENDMENT:
        # Well-formed fields, so the only thing wrong is the pair itself.
        body |= {"info_available_date": TODAY.isoformat(), "elements": ["impact"]}
    return body


def move(run: Run, inc: Tracked, to: str) -> dict[str, Any]:
    reply = run.call("POST", TRANSITIONS, APPROVER, json=transition_body(inc, to),
                     incident_id=inc.id)
    incident = expect(reply, 200)
    inc.update(incident)
    return incident


def incident_in(run: Run, state: str, n: int) -> Tracked:
    """A new incident with one evidence item, moved to `state` along the allowed path."""
    inc = create(run, n)
    import_items(run, inc, "ticketing", [ticket(n)])
    if state != UA:
        determine(run, inc, "Material" if state in (DM, DELAYED) else "NotMaterial")
    if state in (DELAYED, CLOSED):
        move(run, inc, state)
    if inc.state != state:
        raise Fail(f"Couldn't reach {state}: the target reports {inc.state}")
    return inc


def rejected(
    run: Run, method: str, template: str, actor: Actor | None, body: Any,
    status: int, error: str, field: str | None = None, **path: str,
) -> str:
    """Send a call that must be rejected and check it changed no stored data."""
    before = run.baseline()
    kwargs = {} if body is NO_BODY else {"json": body}
    reply = run.call(method, template, actor, **kwargs, **path)
    expect(reply, status, error, field)
    run.unchanged(before, reply.label)
    return f"{status} {error}{f' on {field}' if field else ''}; stored data unchanged"


def server_time(reply: Reply) -> datetime:
    value = reply.headers.get("date")
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        raise TargetError("system", f"{reply.label} sent no readable Date header: {value!r}")


def instant(value: str) -> datetime | None:
    """A spec timestamp as an aware datetime; None when it has no UTC offset."""
    stamp = datetime.fromisoformat(value.upper())
    return stamp if stamp.tzinfo is not None else None


def clock_problems(name: str, value: str, reply: Reply) -> list[str]:
    stamp = instant(value)
    if stamp is None:
        return [f"{name} {value} has no UTC offset"]
    gap = abs(stamp - server_time(reply))
    if gap > CLOCK_TOLERANCE:
        return [f"{name} {value} is {gap.total_seconds():.0f} s from the response Date "
                f"{reply.headers['date']}"]
    return []


def problems_or(problems: list[str], prefix: str, message: str) -> str:
    if problems:
        raise Fail(f"{prefix}: {'; '.join(problems)}")
    return message


def name(state: str) -> str:
    return SHORT.get(state, state)


# AC-02 (REQ-02): an imported item records its source, the importing user and server time.


def ac02() -> list[Case]:
    def subject(run: Run) -> Tracked:
        return run.once("ac02", lambda: create(run, 0))

    def imported(run: Run, source: str, item: dict[str, Any], actor: Actor) -> tuple:
        def build() -> tuple:
            inc = subject(run)
            reply = run.call("POST", IMPORT, actor, json={"source": source, "items": [item]},
                             incident_id=inc.id)
            evidence = expect(reply, 201)["evidence"]
            if len(evidence) != 1:
                raise Fail(f"{reply.label}: sent 1 item, got {len(evidence)} back")
            inc.evidence.append(evidence[0]["evidence_id"])
            return reply, evidence[0]
        return run.once(f"ac02-{source}", build)

    def recorded(source: str, item: dict[str, Any], actor: Actor):
        def case(run: Run) -> str:
            reply, evidence = imported(run, source, item, actor)
            problems = []
            if evidence["source"] != source:
                problems.append(f"source is {evidence['source']}")
            if evidence["imported_by"] != actor.user:
                problems.append(f"imported_by is {evidence['imported_by']}, not {actor.user}")
            problems += clock_problems("imported_at", evidence["imported_at"], reply)
            return problems_or(problems, reply.label, f"source {source}, imported_by {actor.user}, "
                               "imported_at within 5 s of the response Date")
        return case

    def retrievable(run: Run) -> str:
        sent = [imported(run, "ticketing", ticket(0), RESPONDER)[1],
                imported(run, "siem", alert(0), APPROVER)[1]]
        stored = {e["evidence_id"]: e for e in get(run, subject(run).id, BOARD)["evidence"]}
        problems = []
        for evidence in sent:
            found = stored.get(evidence["evidence_id"])
            if found is None:
                problems.append(f"{evidence['evidence_id']} is missing")
                continue
            for key in ("source", "imported_by", "source_ref"):
                if found[key] != evidence[key]:
                    problems.append(f"{evidence['evidence_id']} {key} reads back as {found[key]}")
            if instant(found["imported_at"]) != instant(evidence["imported_at"]):
                problems.append(f"{evidence['evidence_id']} imported_at reads back as "
                                f"{found['imported_at']}")
        return problems_or(problems, "GET as Board",
                           "Both items read back with the same source, imported_by and imported_at")

    def keeps_version(run: Run) -> str:
        imported(run, "ticketing", ticket(0), RESPONDER)
        imported(run, "siem", alert(0), APPROVER)
        version = get(run, subject(run).id)["version"]
        if version != 1:
            raise Fail(f"Two imports changed the version from 1 to {version}")
        return "Version still 1 after two imports"

    def board_forbidden(run: Run) -> str:
        return rejected(run, "POST", IMPORT, BOARD, {"source": "ticketing", "items": [ticket(1)]},
                        403, "FORBIDDEN_ROLE", incident_id=subject(run).id)

    def unknown_incident(run: Run) -> str:
        reply = run.call("POST", IMPORT, RESPONDER, json={"source": "ticketing",
                         "items": [ticket(1)]}, incident_id=UNKNOWN_ID)
        expect(reply, 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    return [
        ("AC-02-ticketing-import-recorded", recorded("ticketing", ticket(0), RESPONDER)),
        ("AC-02-siem-import-by-approver-recorded", recorded("siem", alert(0), APPROVER)),
        ("AC-02-imports-retrievable", retrievable),
        ("AC-02-import-keeps-version", keeps_version),
        ("AC-02-import-as-Board-forbidden", board_forbidden),
        ("AC-02-import-unknown-incident", unknown_incident),
    ]


# AC-03 (REQ-03): 20 ticketing and 20 SIEM items normalize to valid EvidenceItems.


def ac03() -> list[Case]:
    def subject(run: Run) -> Tracked:
        return run.once("ac03", lambda: create(run, 1))

    def batch(run: Run, source: str) -> tuple[list, list]:
        items = [ticket(n) if source == "ticketing" else alert(n) for n in range(20)]
        return items, run.once(f"ac03-{source}",
                               lambda: import_items(run, subject(run), source, items))

    def normalized(source: str):
        ref_key, time_key = ("ticket_id", "created_at") if source == "ticketing" else (
            "alert_id", "detected_at")

        def case(run: Run) -> str:
            items, evidence = batch(run, source)
            by_ref = {e["source_ref"]: e for e in evidence}
            problems = []
            for item in items:
                found = by_ref.get(item[ref_key])
                if found is None:
                    problems.append(f"no item has source_ref {item[ref_key]}")
                elif found["source"] != source:
                    problems.append(f"{item[ref_key]} has source {found['source']}")
                elif instant(found["observed_at"]) != instant(item[time_key]):
                    problems.append(f"{item[ref_key]} observed_at is {found['observed_at']}, "
                                    f"not {time_key} {item[time_key]}")
            return problems_or(problems, f"{source} import",
                               f"20/20 valid EvidenceItems with source {source}, source_ref from "
                               f"{ref_key} and observed_at from {time_key}")
        return case

    def all_retrievable(run: Run) -> str:
        sent = [e["evidence_id"] for e in batch(run, "ticketing")[1] + batch(run, "siem")[1]]
        stored = get(run, subject(run).id)["evidence"]
        counts = {s: sum(e["source"] == s for e in stored) for s in ("ticketing", "siem")}
        problems = []
        if sorted(e["evidence_id"] for e in stored) != sorted(sent):
            problems.append(f"the incident lists {len(stored)} items, not the 40 imported")
        if counts != {"ticketing": 20, "siem": 20}:
            problems.append(f"sources are {counts}")
        if any(e["incident_id"] != subject(run).id for e in stored):
            problems.append("some items name another incident")
        return problems_or(problems, "GET", "40/40 items read back: 20 ticketing, 20 siem")

    def distinct_ids(run: Run) -> str:
        sent = [e["evidence_id"] for e in batch(run, "ticketing")[1] + batch(run, "siem")[1]]
        if len(set(sent)) != 40:
            raise Fail(f"40 items got {len(set(sent))} distinct evidence IDs")
        return "40 distinct evidence IDs"

    def bad_batch(source: str, items: list[dict[str, Any]], field: str):
        def case(run: Run) -> str:
            return rejected(run, "POST", IMPORT, RESPONDER, {"source": source, "items": items},
                            400, "VALIDATION_ERROR", field, incident_id=subject(run).id)
        return case

    return [
        ("AC-03-ticketing-20-items-normalized", normalized("ticketing")),
        ("AC-03-siem-20-items-normalized", normalized("siem")),
        ("AC-03-40-items-retrievable", all_retrievable),
        ("AC-03-evidence-ids-distinct", distinct_ids),
        ("AC-03-reject-unknown-severity",
         bad_batch("siem", [alert(0), {**alert(1), "severity": "urgent"}, alert(2)],
                   "items[1].severity")),
        ("AC-03-reject-malformed-timestamp",
         bad_batch("ticketing", [ticket(0), {**ticket(1), "created_at": "2026-13-01T09:00:00Z"}],
                   "items[1].created_at")),
        ("AC-03-reject-siem-item-as-ticketing", bad_batch("ticketing", [alert(0)], "items[0]")),
        ("AC-03-reject-unknown-source", bad_batch("email", [ticket(0)], "source")),
        ("AC-03-reject-empty-items", bad_batch("ticketing", [], "items")),
    ]


# AC-07 (REQ-07): a determination needs every field and 1+ of the incident's own evidence.


def ac07() -> list[Case]:
    def subject(run: Run) -> Tracked:
        def build() -> Tracked:
            inc = create(run, 2)
            import_items(run, inc, "ticketing", [ticket(4), ticket(5)])
            return inc
        return run.once("ac07", build)

    def other(run: Run) -> Tracked:
        def build() -> Tracked:
            inc = create(run, 3)
            import_items(run, inc, "ticketing", [ticket(6)])
            return inc
        return run.once("ac07-other", build)

    def bad(field: str, change):
        def case(run: Run) -> str:
            inc = subject(run)
            body = determination(inc)
            change(run, inc, body)
            return rejected(run, "POST", DETERMINATION, APPROVER, body, 400, "VALIDATION_ERROR",
                            field, incident_id=inc.id)
        return case

    def missing(field: str):
        return bad(field, lambda run, inc, body: body.pop(field))

    def setting(field: str, value):
        def change(run: Run, inc: Tracked, body: dict[str, Any]) -> None:
            body[field] = value(run, inc) if callable(value) else value
        return bad(field, change)

    def material(run: Run) -> tuple[dict, dict]:
        def build() -> tuple[dict, dict]:
            inc = subject(run)
            body = determination(inc)
            reply = run.call("POST", DETERMINATION, APPROVER, json=body, incident_id=inc.id)
            incident = expect(reply, 200)
            inc.update(incident)
            return body, incident
        return run.once("ac07-material", build)

    def material_recorded(run: Run) -> str:
        body, incident = material(run)
        problems = determination_problems(body, incident, DM)
        return problems_or(problems, "Material determination",
                           f"200; state {DM}, version {body['version']} → {body['version'] + 1}, "
                           "determination stored as sent")

    def required_form(run: Run) -> str:
        incident = material(run)[1]
        if incident.get("required_form") != "8-K Item 1.05":
            raise Fail(f"required_form is {incident.get('required_form')!r}, not '8-K Item 1.05' "
                       "for a Domestic Material determination")
        return "required_form 8-K Item 1.05 (Domestic, Material)"

    def retrievable(run: Run) -> str:
        incident = material(run)[1]
        stored = get(run, subject(run).id, RESPONDER)
        if stored["state"] != DM or stored.get("determination") != incident.get("determination"):
            raise Fail(f"GET returns state {stored['state']} and determination "
                       f"{stored.get('determination')}")
        return "GET as Responder returns the same determination and state"

    def second(run: Run) -> str:
        material(run)
        inc = subject(run)
        return rejected(run, "POST", DETERMINATION, APPROVER, determination(inc), 409,
                        "INVALID_TRANSITION", incident_id=inc.id)

    def not_material(run: Run) -> str:
        inc = create(run, 4)
        import_items(run, inc, "ticketing", [ticket(7), ticket(8)])
        body = determination(inc, "NotMaterial", refs=list(inc.evidence))
        incident = expect(run.call("POST", DETERMINATION, APPROVER, json=body,
                                   incident_id=inc.id), 200)
        problems = determination_problems(body, incident, DNM)
        if incident.get("required_form") is not None:
            problems.append(f"required_form is {incident['required_form']}, not null")
        return problems_or(problems, "NotMaterial determination",
                           f"200; state {DNM} with both refs, required_form null")

    def unknown_incident(run: Run) -> str:
        # No ref can belong to an incident that doesn't exist, so the incident is the one problem.
        body = {**determination(subject(run)), "evidence_refs": [UNKNOWN_ID]}
        expect(run.call("POST", DETERMINATION, APPROVER, json=body, incident_id=UNKNOWN_ID),
               404, "NOT_FOUND")
        return "404 NOT_FOUND"

    def own(run: Run, inc: Tracked) -> str:
        return inc.evidence[0]

    def foreign(run: Run, inc: Tracked) -> str:
        return other(run).evidence[0]

    fields = ["decision", "determination_date", "rationale", "evidence_refs", "version"]
    return [
        *((f"AC-07-missing-{field}", missing(field)) for field in fields),
        ("AC-07-empty-evidence-refs", setting("evidence_refs", [])),
        ("AC-07-ref-from-other-incident",
         setting("evidence_refs", lambda run, inc: [foreign(run, inc)])),
        ("AC-07-unknown-evidence-ref", setting("evidence_refs", [UNKNOWN_ID])),
        ("AC-07-duplicate-evidence-refs",
         setting("evidence_refs", lambda run, inc: [own(run, inc), own(run, inc)])),
        ("AC-07-own-and-other-incident-refs",
         setting("evidence_refs", lambda run, inc: [own(run, inc), foreign(run, inc)])),
        ("AC-07-empty-rationale", setting("rationale", "")),
        ("AC-07-unknown-decision", setting("decision", "Undecided")),
        ("AC-07-impossible-determination-date", setting("determination_date", IMPOSSIBLE_DATE)),
        ("AC-07-version-zero", setting("version", 0)),
        ("AC-07-material-recorded", material_recorded),
        ("AC-07-material-sets-required-form", required_form),
        ("AC-07-determination-retrievable", retrievable),
        ("AC-07-second-determination-rejected", second),
        ("AC-07-not-material-recorded", not_material),
        ("AC-07-unknown-incident", unknown_incident),
    ]


def determination_problems(body: dict[str, Any], incident: dict[str, Any], state: str) -> list:
    problems = []
    if incident["state"] != state:
        problems.append(f"state is {incident['state']}, not {state}")
    if incident["version"] != body["version"] + 1:
        problems.append(f"version is {incident['version']}, not {body['version'] + 1}")
    stored = incident.get("determination") or {}
    if not stored:
        return problems + ["no determination in the response"]
    for key in ("decision", "determination_date", "rationale"):
        if stored[key] != body[key]:
            problems.append(f"{key} is {stored[key]!r}, not {body[key]!r}")
    if sorted(stored["evidence_refs"]) != sorted(body["evidence_refs"]):
        problems.append(f"evidence_refs is {stored['evidence_refs']}")
    if stored["recorded_by"] != APPROVER.user:
        problems.append(f"recorded_by is {stored['recorded_by']}, not {APPROVER.user}")
    return problems


# AC-08 (REQ-07): only a DisclosureApprover can record a determination.


def ac08() -> list[Case]:
    def subject(run: Run) -> Tracked:
        def build() -> Tracked:
            inc = create(run, 5)
            import_items(run, inc, "ticketing", [ticket(9)])
            return inc
        return run.once("ac08", build)

    def refused(actor: Actor, status: int, error: str):
        def case(run: Run) -> str:
            inc = subject(run)
            return rejected(run, "POST", DETERMINATION, actor, determination(inc), status, error,
                            incident_id=inc.id)
        return case

    def approver(run: Run) -> str:
        inc = subject(run)
        body = determination(inc)
        incident = expect(run.call("POST", DETERMINATION, APPROVER, json=body,
                                   incident_id=inc.id), 200)
        return problems_or(determination_problems(body, incident, DM), "As DisclosureApprover",
                           f"200; state {DM}, recorded_by {APPROVER.user}")

    return [
        ("AC-08-Responder-forbidden", refused(RESPONDER, 403, "FORBIDDEN_ROLE")),
        ("AC-08-Board-forbidden", refused(BOARD, 403, "FORBIDDEN_ROLE")),
        ("AC-08-unknown-role-rejected",
         refused(Actor("Administrator", "tester-admin"), 400, "VALIDATION_ERROR")),
        ("AC-08-missing-role-rejected",
         refused(Actor(None, APPROVER.user), 400, "VALIDATION_ERROR")),
        ("AC-08-missing-user-id-rejected",
         refused(Actor(APPROVER.role, None), 400, "VALIDATION_ERROR")),
        ("AC-08-DisclosureApprover-allowed", approver),
    ]


# AC-20 (REQ-19): missing fields and out-of-range dates are rejected by field, storing nothing.


def ac20() -> list[Case]:
    def bad_create(body: dict[str, Any], field: str):
        def case(run: Run) -> str:
            return rejected(run, "POST", INCIDENTS, RESPONDER, body, 400, "VALIDATION_ERROR",
                            field)
        return case

    def create_today(run: Run) -> str:
        body = {"title": "Discovered on the session's today", "discovery_date": TODAY.isoformat()}
        incident = expect(run.call("POST", INCIDENTS, RESPONDER, json=body), 201)
        if incident["discovery_date"] != body["discovery_date"] or incident["state"] != UA:
            raise Fail(f"Stored discovery_date {incident['discovery_date']}, "
                       f"state {incident['state']}")
        return f"201; discovery_date {TODAY} (today) accepted"

    def import_subject(run: Run) -> Tracked:
        return run.once("ac20-import", lambda: create(run, 6))

    def bad_import(body: dict[str, Any], field: str):
        def case(run: Run) -> str:
            return rejected(run, "POST", IMPORT, RESPONDER, body, 400, "VALIDATION_ERROR", field,
                            incident_id=import_subject(run).id)
        return case

    def without(item: dict[str, Any], key: str) -> dict[str, Any]:
        return {k: v for k, v in item.items() if k != key}

    def transition_subject(run: Run) -> Tracked:
        return run.once("ac20-transition", lambda: incident_in(run, DM, 7))

    def bad_transition(field: str):
        def case(run: Run) -> str:
            inc = transition_subject(run)
            body = without(transition_body(inc, DELAYED), field)
            return rejected(run, "POST", TRANSITIONS, APPROVER, body, 400, "VALIDATION_ERROR",
                            field, incident_id=inc.id)
        return case

    def dated_subject(run: Run, key: str, n: int) -> Tracked:
        return run.once(key, lambda: incident_in(run, UA, n))

    def bad_date(offset_from_discovery: int | None):
        def case(run: Run) -> str:
            inc = dated_subject(run, "ac20-dates", 8)
            day = TOMORROW if offset_from_discovery is None else (
                inc.discovery + timedelta(days=offset_from_discovery))
            return rejected(run, "POST", DETERMINATION, APPROVER, determination(inc, day=day), 400,
                            "VALIDATION_ERROR", "determination_date", incident_id=inc.id)
        return case

    def good_date(key: str, n: int, on_discovery: bool):
        def case(run: Run) -> str:
            inc = dated_subject(run, key, n)
            day = inc.discovery if on_discovery else TODAY
            incident = determine(run, inc, day=day)
            stored = (incident.get("determination") or {}).get("determination_date")
            if incident["state"] != DM or stored != day.isoformat():
                raise Fail(f"Stored determination_date {stored}, state {incident['state']}")
            return f"200; determination_date {day} ({'discovery' if on_discovery else 'today'})"
        return case

    title, discovery = incident_body(0)["title"], incident_body(0)["discovery_date"]
    items3 = [ticket(10), ticket(11), ticket(12)]
    alerts3 = [alert(10), alert(11), alert(12)]
    return [
        ("AC-20-create-missing-title", bad_create({"discovery_date": discovery}, "title")),
        ("AC-20-create-missing-discovery_date", bad_create({"title": title}, "discovery_date")),
        ("AC-20-create-empty-title",
         bad_create({"title": "", "discovery_date": discovery}, "title")),
        ("AC-20-create-discovery-tomorrow",
         bad_create({"title": title, "discovery_date": TOMORROW.isoformat()}, "discovery_date")),
        ("AC-20-create-impossible-discovery-date",
         bad_create({"title": title, "discovery_date": IMPOSSIBLE_DATE}, "discovery_date")),
        ("AC-20-create-discovery-today-accepted", create_today),
        ("AC-20-import-missing-source", bad_import({"items": items3}, "source")),
        ("AC-20-import-missing-items", bad_import({"source": "ticketing"}, "items")),
        *((f"AC-20-import-ticketing-missing-{key}",
           bad_import({"source": "ticketing", "items": [*items3[:2], without(items3[2], key)]},
                      f"items[2].{key}"))
          for key in ("ticket_id", "summary", "created_at")),
        *((f"AC-20-import-siem-missing-{key}",
           bad_import({"source": "siem", "items": [*alerts3[:2], without(alerts3[2], key)]},
                      f"items[2].{key}"))
          for key in ("alert_id", "rule_name", "severity", "detected_at")),
        ("AC-20-transition-missing-to", bad_transition("to")),
        ("AC-20-transition-missing-version", bad_transition("version")),
        ("AC-20-determination-before-discovery", bad_date(-1)),
        ("AC-20-determination-after-today", bad_date(None)),
        ("AC-20-determination-on-discovery-accepted", good_date("ac20-dates", 8, True)),
        ("AC-20-determination-on-today-accepted", good_date("ac20-today", 9, False)),
    ]


# AC-06 (REQ-06): all 49 state pairs; Phase 3 runs the 33 it can reach.


def ac06() -> list[Case]:
    # Fixture incident per source state; a source's allowed move runs after its refused ones.
    reachable = {UA: 0, DM: 2, DELAYED: 3, DNM: 4, CLOSED: 5}

    def subject(run: Run, source: str) -> Tracked:
        return run.once(f"ac06-{source}", lambda: incident_in(run, source, reachable[source]))

    def refused(source: str, to: str, route: str = TRANSITIONS):
        def case(run: Run) -> str:
            inc = subject(run, source)
            if route == TRANSITIONS:
                reply = run.call("POST", TRANSITIONS, APPROVER, json=transition_body(inc, to),
                                 incident_id=inc.id)
            else:
                decision = "Material" if to == DM else "NotMaterial"
                reply = run.call("POST", DETERMINATION, APPROVER,
                                 json=determination(inc, decision), incident_id=inc.id)
            after = get(run, inc.id)
            moved = after["state"] != source or after["version"] != inc.version
            if moved:
                # Later cases from this source need a new incident still in it.
                run.forget(f"ac06-{source}")
            expect(reply, 409, "INVALID_TRANSITION")
            if moved:
                raise Fail(f"409 returned, but the incident is now {after['state']} at version "
                           f"{after['version']}")
            return f"409 INVALID_TRANSITION; still {source} at version {inc.version}"
        return case

    def allowed(source: str, to: str):
        def case(run: Run) -> str:
            inc = subject(run, source)
            run.forget(f"ac06-{source}")
            before = get(run, inc.id)
            version = inc.version
            if (source, to) in DETERMINATION_MOVES:
                route = "/determination"
                incident = determine(run, inc, "Material" if to == DM else "NotMaterial")
            else:
                route = "/transitions"
                incident = move(run, inc, to)
            problems = []
            if incident["state"] != to:
                problems.append(f"state is {incident['state']}")
            if incident["version"] != version + 1:
                problems.append(f"version went from {version} to {incident['version']}")
            if route == "/transitions" and incident.get("determination") != before.get(
                    "determination"):
                problems.append("the transition changed the determination")
            return problems_or(problems, f"{name(source)} → {name(to)}",
                               f"200 through {route}; {to} at version {version + 1}")
        return case

    def guarded(source: str, actor: Actor, body, status: int, error: str, field=None):
        def case(run: Run) -> str:
            inc = subject(run, source)
            try:
                return rejected(run, "POST", TRANSITIONS, actor, body(inc), status, error, field,
                                incident_id=inc.id)
            except Fail:
                run.forget(f"ac06-{source}")
                raise
        return case

    def skipped(reason: str):
        def case(run: Run) -> str:
            raise Skip(reason)
        return case

    def unknown_incident(run: Run) -> str:
        expect(run.call("POST", TRANSITIONS, APPROVER, json={"to": CLOSED, "version": 1},
                        incident_id=UNKNOWN_ID), 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    allowed_pairs = DETERMINATION_MOVES | TRANSITION_MOVES
    cases: list[Case] = []
    for source in reachable:
        targets = [to for to in STATES if (source, to) not in allowed_pairs | FILING_MOVES]
        cases += [(f"AC-06-{name(source)}-to-{name(to)}", refused(source, to)) for to in targets]
        if source == UA:
            cases += [(f"AC-06-UA-to-{name(to)}-via-transitions", refused(UA, to))
                      for to in (DM, DNM)]
        if source == DNM:
            cases.append(("AC-06-DNM-to-DM-via-determination", refused(DNM, DM, DETERMINATION)))
        if source == DM:
            cases += [
                ("AC-06-transition-as-Responder-forbidden",
                 guarded(DM, RESPONDER, lambda inc: transition_body(inc, DELAYED), 403,
                         "FORBIDDEN_ROLE")),
                ("AC-06-transition-as-Board-forbidden",
                 guarded(DM, BOARD, lambda inc: transition_body(inc, DELAYED), 403,
                         "FORBIDDEN_ROLE")),
                ("AC-06-unknown-target-state",
                 guarded(DM, APPROVER, lambda inc: {"to": "Archived", "version": inc.version},
                         400, "VALIDATION_ERROR", "to")),
            ]
        for to in STATES:
            if (source, to) in FILING_MOVES:
                cases.append((f"AC-06-{name(source)}-to-{name(to)}-via-transitions",
                              refused(source, to)))
                cases.append((f"AC-06-{name(source)}-to-{name(to)}",
                              skipped(f"{source} → {to} happens only through /filings, which is "
                                      "Phase 4; not run in Phase 3")))
        cases += [(f"AC-06-{name(source)}-to-{name(to)}", allowed(source, to))
                  for to in STATES if (source, to) in allowed_pairs]
    for source in (RECORDED, AMENDMENT):
        cases += [(f"AC-06-{name(source)}-to-{name(to)}",
                   skipped(f"Reaching {source} needs a recorded filing, which is Phase 4; not "
                           "run in Phase 3"))
                  for to in STATES]
    cases.append(("AC-06-unknown-incident", unknown_incident))
    return cases


# AC-09 (a) (REQ-08): no route but the determination one sets a materiality value.


def ac09() -> list[Case]:
    def filing_body(inc: Tracked) -> dict[str, Any]:
        return {"form_type": "8-K Item 1.05", "filing_date": TODAY.isoformat(),
                "accession_number": "0000000000-26-000001", "version": inc.version}

    def case_for(n: int):
        def case(run: Run) -> str:
            inc = create(run, n)
            import_items(run, inc, "ticketing", [ticket(2 * n), ticket(2 * n + 1)])
            evidence_id = inc.evidence[0]
            calls = [
                ("POST", DRAFTS, APPROVER, {}),
                *(("POST", TRANSITIONS, APPROVER, {"json": transition_body(inc, to)})
                  for to in STATES),
                ("POST", FILINGS, APPROVER, {"json": filing_body(inc)}),
                ("PATCH", EVIDENCE, RESPONDER, {"json": {"impact_categories": ["operational"]},
                                                "evidence_id": evidence_id}),
                ("DELETE", EVIDENCE, APPROVER, {"evidence_id": evidence_id}),
                ("PUT", INCIDENT, APPROVER, {}),
                ("DELETE", INCIDENT, APPROVER, {}),
                *((method, AUDIT, APPROVER, {}) for method in ("PUT", "PATCH", "DELETE")),
            ]
            # The outcomes of these calls are other cases' business; only their effect counts.
            replies = [run.call(m, t, actor, incident_id=inc.id, **kw) for m, t, actor, kw in calls]
            draft = replies[0]
            draft_id = draft.body.get("draft_id", UNKNOWN_ID) if (
                draft.status == 201 and isinstance(draft.body, dict)) else UNKNOWN_ID
            run.call("POST", ACCEPT, APPROVER, draft_id=draft_id)
            # A list shared by the ten cases, so each groups its incident with the one before.
            grouped = run.once("ac09-incidents", list)
            run.call("POST", GROUPS, APPROVER,
                     json={"incident_ids": [grouped[-1] if grouped else UNKNOWN_ID, inc.id]})
            grouped.append(inc.id)
            entries = expect(run.call("GET", AUDIT, BOARD, incident_id=inc.id), 200)["entries"]
            incident = get(run, inc.id, RESPONDER)
            problems = []
            if incident.get("determination") is not None:
                problems.append(f"determination is {incident['determination']}")
            if incident["state"] != UA:
                problems.append(f"state is {incident['state']}")
            if any(e["action"] == "record_determination" for e in entries):
                problems.append("the audit trail has a record_determination entry")
            total = len(calls) + 2  # with the draft accept and the group
            return problems_or(problems, f"After {total} non-determination calls",
                               f"After {total} non-determination calls: determination null, "
                               "state UnderAssessment")
        return case

    return [(f"AC-09-a-incident-{n + 1:02d}", case_for(n)) for n in range(10)]


# AC-23 (a)-(c) (REQ-22): every action is audited; audit entries, incidents and evidence
# can't be edited or deleted.


@dataclass
class Step:
    """One action of the AC-23 (a) script and the entry it should leave."""

    action: str
    actor: Actor
    incident_id: str
    reply: Reply
    before: tuple[str, int] | None = None
    after: tuple[str, int] | None = None


def audit_script(run: Run) -> tuple[list[Step], dict[str, list[dict[str, Any]]]]:
    """Run 3 creates, 3 imports, 1 Material determination and 3 transitions; read the trails."""
    steps: list[Step] = []
    incidents = []
    for n, actor in ((0, RESPONDER), (1, APPROVER), (2, RESPONDER_2)):
        reply = run.call("POST", INCIDENTS, actor, json=incident_body(n))
        body = expect(reply, 201)
        incidents.append(Tracked(body["incident_id"], date.fromisoformat(
            incident_body(n)["discovery_date"]), body["state"], body["version"], []))
        steps.append(Step("create_incident", actor, body["incident_id"], reply, after=(UA, 1)))
    imports = (
        (incidents[0], RESPONDER, "ticketing", [ticket(13), ticket(14)]),
        (incidents[1], RESPONDER_2, "siem", [alert(13), alert(14)]),
        (incidents[2], APPROVER, "ticketing", [ticket(15)]),
    )
    for inc, actor, source, items in imports:
        reply = run.call("POST", IMPORT, actor, json={"source": source, "items": items},
                         incident_id=inc.id)
        inc.evidence += [e["evidence_id"] for e in expect(reply, 201)["evidence"]]
        steps.append(Step("import_evidence", actor, inc.id, reply))
    first = incidents[0]
    reply = run.call("POST", DETERMINATION, APPROVER, json=determination(first),
                     incident_id=first.id)
    first.update(expect(reply, 200))
    steps.append(Step("record_determination", APPROVER, first.id, reply, (UA, 1), (DM, 2)))
    for to, version in ((DELAYED, 2), (DM, 3), (DELAYED, 4)):
        before = (first.state, version)
        reply = run.call("POST", TRANSITIONS, APPROVER, json=transition_body(first, to),
                         incident_id=first.id)
        first.update(expect(reply, 200))
        steps.append(Step("transition", APPROVER, first.id, reply, before, (to, version + 1)))
    trails = {
        inc.id: expect(run.call("GET", AUDIT, BOARD, incident_id=inc.id), 200)["entries"]
        for inc in incidents
    }
    return steps, trails


def entry_for(steps: list[Step], trails: dict, k: int) -> dict[str, Any] | None:
    """The entry for step k: the nth entry with its incident and action, n counting from 0."""
    step = steps[k]
    nth = sum((s.incident_id, s.action) == (step.incident_id, step.action) for s in steps[:k])
    matching = [e for e in trails[step.incident_id] if e["action"] == step.action]
    return matching[nth] if nth < len(matching) else None


def entry_problems(step: Step, entry: dict[str, Any]) -> list[str]:
    problems = []
    if entry["user_id"] != step.actor.user:
        problems.append(f"user_id is {entry['user_id']}, not {step.actor.user}")
    if entry["target_id"] != step.incident_id:
        problems.append(f"target_id is {entry['target_id']}")
    problems += clock_problems("timestamp", entry["timestamp"], step.reply)
    if step.action == "create_incident" and entry["before"] is not None:
        problems.append(f"before is {entry['before']}, not null")
    for side, expected in (("before", step.before), ("after", step.after)):
        if expected is None:
            continue
        values = entry[side] if isinstance(entry[side], dict) else {}
        state, version = expected
        if values.get("state") != state:
            problems.append(f"{side}.state is {values.get('state')!r}, not {state}")
        if values.get("version") != version:
            problems.append(f"{side}.version is {values.get('version')!r}, not {version}")
    return problems


def ac23() -> list[Case]:
    def script(run: Run) -> tuple[list[Step], dict]:
        return run.once("ac23-a", lambda: audit_script(run))

    def entry_case(k: int):
        def case(run: Run) -> str:
            steps, trails = script(run)
            step = steps[k]
            entry = entry_for(steps, trails, k)
            if entry is None:
                raise Fail(f"Action {k + 1} ({step.action} on {step.incident_id}) has no entry")
            change = (f", state {step.before[0] if step.before else 'none'} → {step.after[0]}"
                      if step.after else "")
            return problems_or(entry_problems(step, entry), f"Entry for action {k + 1}",
                               f"{step.action} by {step.actor.user}{change}, timestamp within "
                               "5 s of the response Date")
        return case

    def oldest_first(run: Run) -> str:
        steps, trails = script(run)
        problems = []
        for incident_id, entries in trails.items():
            expected = [s.action for s in steps if s.incident_id == incident_id]
            actual = [e["action"] for e in entries]
            if actual != expected:
                problems.append(f"{incident_id} lists {actual}, expected {expected}")
        if not problems:
            stamps = [instant(entry_for(steps, trails, k)["timestamp"]) for k in range(len(steps))]
            if any(a is None or b is None or b < a for a, b in zip(stamps, stamps[1:])):
                problems.append("timestamps don't follow the script order")
        return problems_or(problems, "Audit trails",
                           "Exactly 10 entries, oldest first, in script order")

    def reads_not_audited(run: Run) -> str:
        steps, trails = script(run)
        incident_id = steps[0].incident_id
        get(run, incident_id, RESPONDER)
        again = expect(run.call("GET", AUDIT, RESPONDER, incident_id=incident_id), 200)["entries"]
        if again != trails[incident_id]:
            raise Fail(f"Entries went from {len(trails[incident_id])} to {len(again)} after "
                       "reads")
        return "Reading the incident and its trail added no entries"

    def subject(run: Run, key: str, n: int) -> Tracked:
        return run.once(key, lambda: incident_in(run, UA, n))

    def outcome(role: str) -> tuple[int, str]:
        # The spec's roles rule: only a DisclosureApprover gets as far as the 405.
        return (405, "METHOD_NOT_ALLOWED") if role == APPROVER.role else (403, "FORBIDDEN_ROLE")

    def audit_edit(method: str, role: str):
        def case(run: Run) -> str:
            inc = subject(run, "ac23-b", 3)
            before = expect(run.call("GET", AUDIT, APPROVER, incident_id=inc.id), 200)["entries"]
            reply = run.call(method, AUDIT, ACTORS[role], incident_id=inc.id)
            expect(reply, *outcome(role))
            after = expect(run.call("GET", AUDIT, APPROVER, incident_id=inc.id), 200)["entries"]
            if after != before:
                raise Fail(f"{reply.label} changed the trail from {len(before)} to {len(after)} "
                           "entries")
            return f"{' '.join(map(str, outcome(role)))}; entries unchanged"
        return case

    def record_edit(method: str, template: str, role: str):
        def case(run: Run) -> str:
            inc = subject(run, "ac23-c", 4)
            path = {"incident_id": inc.id}
            if template == EVIDENCE:
                path["evidence_id"] = inc.evidence[0]
            message = rejected(run, method, template, ACTORS[role], NO_BODY, *outcome(role),
                               **path)
            incident = get(run, inc.id, BOARD)
            if inc.evidence[0] not in [e["evidence_id"] for e in incident["evidence"]]:
                raise Fail("The evidence item is no longer retrievable")
            return f"{message}; incident and evidence still retrievable"
        return case

    def unknown_incident(run: Run) -> str:
        expect(run.call("GET", AUDIT, BOARD, incident_id=UNKNOWN_ID), 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    roles = list(ACTORS)
    return [
        *((f"AC-23-a-entry-{k + 1:02d}", entry_case(k)) for k in range(10)),
        ("AC-23-a-ten-entries-oldest-first", oldest_first),
        ("AC-23-a-reads-not-audited", reads_not_audited),
        *((f"AC-23-b-{method}-audit-as-{role}", audit_edit(method, role))
          for method in ("PUT", "PATCH", "DELETE") for role in roles),
        *((f"AC-23-c-DELETE-incident-as-{role}", record_edit("DELETE", INCIDENT, role))
          for role in roles),
        *((f"AC-23-c-DELETE-evidence-as-{role}", record_edit("DELETE", EVIDENCE, role))
          for role in roles),
        ("AC-23-c-PUT-incident-as-DisclosureApprover",
         record_edit("PUT", INCIDENT, APPROVER.role)),
        ("AC-23-unknown-incident-audit", unknown_incident),
    ]


# AC-01 (REQ-01): 100 creates get distinct IDs and are stored as sent, in UnderAssessment.
# It runs last so its incidents don't enlarge every earlier snapshot.


def ac01() -> list[Case]:
    def creates(run: Run) -> list[tuple[dict, Reply]]:
        def build() -> list[tuple[dict, Reply]]:
            return [(body, run.call("POST", INCIDENTS, RESPONDER, json=body))
                    for body in map(incident_body, range(100))]
        return run.once("ac01", build)

    def created(run: Run) -> list[tuple[dict, dict]]:
        done = [(body, reply.body) for body, reply in creates(run)
                if reply.status == 201 and not reply.schema_errors]
        if len(done) != 100:
            raise Fail(f"Only {len(done)}/100 creates returned a valid 201")
        return done

    def hundred(run: Run) -> str:
        failed = [(k, reply) for k, (_, reply) in enumerate(creates(run))
                  if reply.status != 201 or reply.schema_errors]
        if failed:
            k, reply = failed[0]
            detail = reply.schema_errors[0] if reply.status == 201 else (
                f"{reply.status}{reply.detail()}")
            message = (f"{100 - len(failed)}/100 creates returned a valid 201; "
                       f"create {k + 1}: {detail}")
            if any(r.status >= 500 and r.status != 501 for _, r in failed):
                raise TargetError("system", message)
            raise Fail(message)
        return "100/100 creates as Responder returned 201 with a valid Incident"

    def distinct(run: Run) -> str:
        ids = {incident["incident_id"] for _, incident in created(run)}
        if len(ids) != 100:
            raise Fail(f"100 creates got {len(ids)} distinct IDs")
        return "100 distinct incident IDs"

    def stored(run: Run) -> str:
        done = created(run)
        incidents = {i["incident_id"]: i for i in run.snapshot()["incidents"]}
        problems = []
        for body, incident in done:
            found = incidents.get(incident["incident_id"])
            if found is None:
                problems.append(f"{incident['incident_id']} is missing")
            elif (found["title"], found["discovery_date"], found["state"]) != (
                    body["title"], body["discovery_date"], UA):
                problems.append(f"{incident['incident_id']} is stored as {found['title']!r}, "
                                f"{found['discovery_date']}, {found['state']}")
        return problems_or(problems, f"{len(problems)}/100 stored wrongly; first",
                           "All 100 stored with their title and discovery_date, in UnderAssessment")

    def approver_creates(run: Run) -> str:
        body = incident_body(9)
        incident = expect(run.call("POST", INCIDENTS, APPROVER, json=body), 201)
        problems = []
        expected = {"title": body["title"], "discovery_date": body["discovery_date"], "state": UA,
                    "version": 1, "evidence": [], "filings": []}
        problems += [f"{key} is {incident[key]!r}" for key, value in expected.items()
                     if incident[key] != value]
        problems += [f"{key} is {incident[key]!r}, not null"
                     for key in ("determination", "required_form", "due_date", "group_id")
                     if incident.get(key) is not None]
        return problems_or(problems, "New incident",
                           "201 in UnderAssessment at version 1 with no evidence, filings or "
                           "determination")

    def board_forbidden(run: Run) -> str:
        return rejected(run, "POST", INCIDENTS, BOARD, incident_body(0), 403, "FORBIDDEN_ROLE")

    def unknown_incident(run: Run) -> str:
        expect(run.call("GET", INCIDENT, RESPONDER, incident_id=UNKNOWN_ID), 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    return [
        ("AC-01-create-as-Board-forbidden", board_forbidden),
        ("AC-01-create-as-DisclosureApprover-defaults", approver_creates),
        ("AC-01-get-unknown-incident", unknown_incident),
        ("AC-01-100-creates", hundred),
        ("AC-01-distinct-ids", distinct),
        ("AC-01-stored-as-sent", stored),
    ]


def planned() -> list[Case]:
    """Every case in run order; their test IDs are stable and unique."""
    return [*ac02(), *ac03(), *ac07(), *ac08(), *ac20(), *ac06(), *ac09(), *ac23(), *ac01()]


def run_suite(target: Target) -> list[dict[str, Any]]:
    """Run every planned case against a prepared session; one TestResult each."""
    return run_cases(planned(), target)
