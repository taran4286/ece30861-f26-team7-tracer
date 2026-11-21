"""The cross-team test cases, in run order: AC-21, 02, 03, 07, 08, 20, 14, 12, 13, 15, 06, 09,
23, 04, 05, 19, 16, 17, 18, then 01.

Each criterion has normal cases and edge cases (boundaries, bad input, wrong roles, unknown
IDs). Pass rules are the course's acceptance criteria; expected statuses, error codes and
fields come from system-openapi.yaml, and expected due dates from the tester's own calendar.
"""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import cache
from itertools import count
from pathlib import Path
from typing import Any

from tester import stub
from tester.business_days import add_business_days
from tester.client import NO_BODY, Actor, Reply, TargetError
from tester.suite import Case, Fail, Run, expect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

# "Today" is the date of the session's test_now. The tester's setup sends {}, so test_now is
# the spec's default. A case that moves the clock sets it back to this before it ends.
SETUP_TEST_NOW = "2026-10-05T15:00:00Z"
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
TRANSITION_MOVES = {(DM, DELAYED), (DELAYED, DM), (DNM, CLOSED), (RECORDED, AMENDMENT),
                    (RECORDED, CLOSED)}
FILING_MOVES = {(DM, RECORDED), (DELAYED, RECORDED), (AMENDMENT, RECORDED)}

FORM_8K, FORM_8KA, FORM_6K, FORM_801 = "8-K Item 1.05", "8-K/A", "6-K", "8-K Item 8.01"
# The form a filing move files in a Domestic session: the incident's required_form (3.2.2).
FILING_FORMS = {DM: FORM_8K, DELAYED: FORM_8K, AMENDMENT: FORM_8KA}
DUE_FLAGS = ("warning", "overdue")
# The states incident_in reaches through a filing.
FILED = {RECORDED, AMENDMENT}

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

# Drafts (REQ-15 to REQ-17, REQ-20).
ELEMENTS = ("nature", "scope", "timing", "impact")
UNDETERMINED = "Not yet determined"
DRAFTING_UNAVAILABLE = "Drafting unavailable"
# AC-21 (a): an LLM error comes back as "Drafting unavailable" within this many seconds.
STUB_ERROR_SECONDS = 5
IMPACT_CATEGORIES = ["operational", "financial", "legal_regulatory", "reputational",
                     "data_compromised"]


@cache
def _fixture(name: str) -> Any:
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


def create(run: Run, n: int, actor: Actor = RESPONDER, discovery: date | None = None) -> Tracked:
    body = incident_body(n)
    if discovery is not None:
        body["discovery_date"] = discovery.isoformat()
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


def transition_body(inc: Tracked, to: str, available: date = TODAY) -> dict[str, Any]:
    body: dict[str, Any] = {"to": to, "version": inc.version}
    if to == AMENDMENT:
        # Valid fields, impact being what incident_in's filing lists as unavailable, so the
        # only thing wrong in a refused move is the pair itself.
        body |= {"info_available_date": available.isoformat(), "elements": ["impact"]}
    return body


def move(run: Run, inc: Tracked, to: str, available: date = TODAY) -> dict[str, Any]:
    reply = run.call("POST", TRANSITIONS, APPROVER, json=transition_body(inc, to, available),
                     incident_id=inc.id)
    incident = expect(reply, 200)
    inc.update(incident)
    return incident


def accession(run: Run) -> str:
    """A well-formed accession number, distinct within the session."""
    numbers = run.once("accession-numbers", lambda: count(1))
    return f"0000000007-26-{next(numbers):06d}"


def filing_body(
    run: Run, inc: Tracked, form: str, day: date = TODAY, **optional: Any
) -> dict[str, Any]:
    """A valid FilingRequest body for `form`, filed on `day`; `optional` adds the optional
    fields, such as unavailable_elements or home_disclosure_date."""
    return {"form_type": form, "filing_date": day.isoformat(), "accession_number": accession(run),
            "version": inc.version, **optional}


def file(run: Run, inc: Tracked, form: str, **kwargs: Any) -> dict[str, Any]:
    reply = run.call("POST", FILINGS, APPROVER, json=filing_body(run, inc, form, **kwargs),
                     incident_id=inc.id)
    incident = expect(reply, 201)
    inc.update(incident)
    return incident


def incident_in(run: Run, state: str, n: int) -> Tracked:
    """A new incident with one evidence item, moved to `state` along the allowed path.

    Its 8-K filing lists impact as unavailable, so a DisclosureRecorded incident can move on to
    AmendmentPending; the move there sends info_available_date today.
    """
    inc = create(run, n)
    import_items(run, inc, "ticketing", [ticket(n)])
    if state != UA:
        determine(run, inc, "NotMaterial" if state in (DNM, CLOSED) else "Material")
    if state in (DELAYED, CLOSED):
        move(run, inc, state)
    if state in (RECORDED, AMENDMENT):
        file(run, inc, FORM_8K, unavailable_elements=["impact"])
    if state == AMENDMENT:
        move(run, inc, AMENDMENT)
    if inc.state != state:
        raise Fail(f"Couldn't reach {state}: the target reports {inc.state}")
    return inc


@contextmanager
def _set_back(change: Callable[[], None], reset: Callable[[], None], what: str) -> Iterator[None]:
    """Run the block after `change`, then `reset` however the block ends.

    A reset that fails fails the case; when the block already failed it, the reset's failure
    is added to that message. `what` names the reset in those messages.
    """
    try:
        change()
        yield
    except Exception as exc:
        try:
            reset()
        except Exception as error:
            if isinstance(exc, Fail):
                raise Fail(f"{exc}; also couldn't set {what}: {error}") from exc
        raise
    try:
        reset()
    except Fail as exc:
        raise Fail(f"Couldn't set {what}: {exc}")


def clock_at(run: Run, day: date):
    """Run the block with the session's test_now at 15:00 UTC on `day`, then set it back to the
    setup default, since later cases assume TODAY."""
    return _set_back(lambda: run.set_clock(f"{day.isoformat()}T15:00:00Z"),
                     lambda: run.set_clock(SETUP_TEST_NOW), f"test_now back to {SETUP_TEST_NOW}")


def tester_url(run: Run) -> str:
    if run.tester_url is None:
        raise RuntimeError("the run has no tester URL, so the target can't reach the LLM stub")
    return run.tester_url


def failure_stub(run: Run) -> str:
    """The always-failing LLM stub, every session's llm_base_url outside llm_at (AC-21 (c))."""
    return stub.fail_url(tester_url(run))


def llm_at(run: Run, content: dict[str, Any] | str):
    """Run the block with the session's LLM a stub replying `content` (JSON for a dict), then
    set it back to the failure stub."""
    text = content if isinstance(content, str) else json.dumps(content)
    return _set_back(lambda: run.set_llm(stub.reply_url(tester_url(run), text)),
                     lambda: run.set_llm(failure_stub(run)),
                     "llm_base_url back to the failure stub")


def expected_due(start: date, stated: date) -> date:
    """`start` + 4 business days by the tester's own calendar, which must give the date the
    criterion states; a calendar that disagrees is a tester fault."""
    due = add_business_days(start)
    if due != stated:
        raise RuntimeError(f"the tester's calendar gives {due} for {start} + 4 business days, "
                           f"not {stated}")
    return due


def flag_problems(where: str, incident: dict[str, Any], expected: set[str]) -> list[str]:
    """A problem unless the incident's due-date flags are exactly `expected`; the spec checks
    them in order, so at most one applies."""
    flags = {flag for flag in incident["flags"] if flag in DUE_FLAGS}
    if flags == expected:
        return []
    return [f"{where} has {describe(flags)}, not {describe(expected)}"]


def describe(flags: set[str]) -> str:
    return " and ".join(sorted(flags)) or "neither warning nor overdue"


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


def instant(value: str) -> datetime | None:
    """A spec timestamp as an aware datetime; None when it has no UTC offset."""
    stamp = datetime.fromisoformat(value.upper())
    return stamp if stamp.tzinfo is not None else None


def clock_problems(name: str, value: str, reply: Reply) -> list[str]:
    """Real server time: within 5 s of when the target handled the call.

    The tester can't read the target's clock, so it brackets the call with its own, both being
    NTP-synced UTC. The response's Date header isn't used: a host can send a stale one, as a
    Vercel function does on its first reply after sitting idle.
    """
    stamp = instant(value)
    if stamp is None:
        return [f"{name} {value} has no UTC offset"]
    if stamp < reply.sent_at - CLOCK_TOLERANCE:
        gap = (reply.sent_at - stamp).total_seconds()
        return [f"{name} {value} is {gap:.0f} s before the request was sent at "
                f"{reply.sent_at.isoformat()}"]
    if stamp > reply.received_at + CLOCK_TOLERANCE:
        gap = (stamp - reply.received_at).total_seconds()
        return [f"{name} {value} is {gap:.0f} s after the response arrived at "
                f"{reply.received_at.isoformat()}"]
    return []


def problems_or(problems: list[str], prefix: str, message: str) -> str:
    if problems:
        raise Fail(f"{prefix}: {'; '.join(problems)}")
    return message


def name(state: str) -> str:
    return SHORT.get(state, state)


# Drafts. Each case decides what the model says through the tester's LLM stub (see llm_at).


def material(run: Run, n: int, items: list[dict[str, Any]]) -> Tracked:
    """A new Domestic Material incident whose evidence is these ticketing items."""
    inc = create(run, n)
    import_items(run, inc, "ticketing", items)
    determine(run, inc)
    return inc


def drafting_subject(run: Run) -> Tracked:
    """The Material incident AC-21, AC-09 (c), AC-16's and AC-17's faults and AC-18 draft."""
    return run.once("drafting", lambda: material(run, 0, [ticket(18)]))


def element(text: str, citations: list[str]) -> dict[str, Any]:
    """A DraftElement as a model would send it, with no sensitive flags of its own."""
    return {"text": text, "citations": list(citations), "sensitive_flags": []}


def model_reply(**elements: dict[str, Any]) -> dict[str, Any]:
    """The model's four elements: those given, the rest Not yet determined."""
    return {name: elements.get(name, element(UNDETERMINED, [])) for name in ELEMENTS}


def draft(run: Run, inc: Tracked, content: dict[str, Any] | str | None = None) -> Reply:
    """Request a draft as DisclosureApprover, the model replying `content`; with None the
    session keeps the failure stub."""
    if content is None:
        return run.call("POST", DRAFTS, APPROVER, incident_id=inc.id)
    with llm_at(run, content):
        return run.call("POST", DRAFTS, APPROVER, incident_id=inc.id)


def materiality_fields(value: Any, path: str = "$") -> list[str]:
    """Where a value has a key named materiality, which the spec says no draft contains."""
    if isinstance(value, dict):
        own = [f"{path}.{key}" for key in value if str(key).lower() == "materiality"]
        return own + [f for key, item in value.items()
                      for f in materiality_fields(item, f"{path}.{key}")]
    if isinstance(value, list):
        return [f for k, item in enumerate(value) for f in materiality_fields(item, f"{path}[{k}]")]
    return []


def structure_problems(draft_body: dict[str, Any], inc: Tracked) -> list[str]:
    """AC-16's structure: this incident's Proposed advisory draft of its form, each of the
    four elements with text or exactly Not yet determined, and no materiality field."""
    problems = []
    if draft_body["incident_id"] != inc.id:
        problems.append(f"incident_id is {draft_body['incident_id']}, not {inc.id}")
    if draft_body["form_type"] != FORM_8K:
        problems.append(f"form_type is {draft_body['form_type']!r}, not {FORM_8K!r}")
    if draft_body["status"] != "Proposed":
        problems.append(f"status is {draft_body['status']!r}, not 'Proposed'")
    problems += [f"it has a materiality field at {f}" for f in materiality_fields(draft_body)]
    for name in ELEMENTS:
        if not draft_body["elements"][name]["text"].strip():
            problems.append(f"{name} has no text, not even {UNDETERMINED!r}")
    return problems


def citation_problems(draft_body: dict[str, Any], inc: Tracked) -> list[str]:
    """AC-17 (a) and the spec: drafted text cites at least one of this incident's evidence IDs
    and nothing else; Not yet determined has no citations or flags."""
    problems = []
    for name in ELEMENTS:
        item = draft_body["elements"][name]
        if item["text"] == UNDETERMINED:
            if item["citations"] or item["sensitive_flags"]:
                problems.append(f"{name} is {UNDETERMINED!r} with citations "
                                f"{item['citations']} and flags {item['sensitive_flags']}")
            continue
        if not item["citations"]:
            problems.append(f"{name} has text but no citations")
        if foreign := [c for c in item["citations"] if c not in inc.evidence]:
            problems.append(f"{name} cites {foreign}, not evidence of this incident")
    return problems


def unavailable(reply: Reply) -> str:
    """Pass a 503 DRAFTING_UNAVAILABLE whose message is exactly Drafting unavailable."""
    body = expect(reply, 503, "DRAFTING_UNAVAILABLE")
    if body["message"] != DRAFTING_UNAVAILABLE:
        raise Fail(f"{reply.label}: 503 message is {body['message']!r}, not "
                   f"{DRAFTING_UNAVAILABLE!r}")
    return f"503 {DRAFTING_UNAVAILABLE}"


def not_passed_on(run: Run, inc: Tracked, content: dict[str, Any], what: str) -> str:
    """Draft with a reply the system must not pass on as sent. A 503 Drafting unavailable
    passes, and so does a 201 draft that keeps the spec's rules without it; either way the
    incident's state and version stay as they were."""
    reply = draft(run, inc, content)
    if reply.status == 201:
        body = expect(reply, 201)
        problems = structure_problems(body, inc) + citation_problems(body, inc)
        message = problems_or(problems, f"{reply.label} drafted {what}",
                              f"201 draft without {what}, keeping the spec's rules")
    else:
        message = f"{unavailable(reply)} for {what}"
    return f"{message}; {incident_kept(run, inc, reply.label)}"


def incident_kept(run: Run, inc: Tracked, label: str) -> str:
    """Fail unless the incident still has the state and version the tester last saw; a draft
    request changes neither."""
    incident = get(run, inc.id)
    if (incident["state"], incident["version"]) != (inc.state, inc.version):
        raise Fail(f"{label} moved the incident from {inc.state} at version {inc.version} to "
                   f"{incident['state']} at version {incident['version']}")
    return f"still {inc.state} at version {inc.version}"


# AC-21 (a) and (c) (REQ-20): an LLM failure is "Drafting unavailable" within 5 s, and the
# rest works without an LLM. The session's LLM is the tester's failure stub from the first case
# on, and every case that drafts sets it back, so AC-01 to AC-15's cases run with it down. AC-21
# (b), no answer within 60 s, is left to the system's own tests: a call that long breaks the
# pod's 10 s project call limit.


def ac21() -> list[Case]:
    def stub_down(run: Run) -> str:
        url = failure_stub(run)
        run.set_llm(url)
        return (f"llm_base_url set to the tester's failure stub, {url}, for every case outside "
                "a draft; AC-01 to AC-15's cases run with it down")

    def unavailable_within(content: str | None, cause: str):
        def case(run: Run) -> str:
            inc = drafting_subject(run)
            reply = draft(run, inc, content)
            message = unavailable(reply)
            took = (reply.received_at - reply.sent_at).total_seconds()
            if took > STUB_ERROR_SECONDS:
                raise Fail(f"{message} took {took:.1f} s, not {STUB_ERROR_SECONDS} s or less")
            return (f"{message} {took:.1f} s after {cause}; "
                    f"{incident_kept(run, inc, reply.label)}")
        return case

    return [
        ("AC-21-c-session-uses-failure-stub", stub_down),
        ("AC-21-a-stub-error-drafting-unavailable",
         unavailable_within(None, "the stub's 500")),
        ("AC-21-a-non-JSON-reply-drafting-unavailable",
         unavailable_within("The model is unavailable.", "a reply that isn't JSON")),
    ]


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
                               "imported_at within 5 s of the call")
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


# AC-14 (REQ-13): a filing is stored with its form, date and accession number and records the
# disclosure; each of the three is required.


def ac14() -> list[Case]:
    def subject(run: Run) -> Tracked:
        return run.once("ac14", lambda: incident_in(run, DM, 1))

    def missing(field: str):
        def case(run: Run) -> str:
            inc = subject(run)
            body = filing_body(run, inc, FORM_8K)
            del body[field]
            return rejected(run, "POST", FILINGS, APPROVER, body, 400, "VALIDATION_ERROR", field,
                            incident_id=inc.id)
        return case

    def filed(run: Run) -> tuple[dict, dict]:
        def build() -> tuple[dict, dict]:
            inc = subject(run)
            body = filing_body(run, inc, FORM_8K)
            incident = expect(run.call("POST", FILINGS, APPROVER, json=body, incident_id=inc.id),
                              201)
            inc.update(incident)
            return body, incident
        return run.once("ac14-filing", build)

    def recorded(run: Run) -> str:
        body, incident = filed(run)
        problems = []
        if incident["state"] != RECORDED:
            problems.append(f"state is {incident['state']}, not {RECORDED}")
        if incident["version"] != body["version"] + 1:
            problems.append(f"version is {incident['version']}, not {body['version'] + 1}")
        return problems_or(problems, "8-K Item 1.05 filing",
                           f"201; state {RECORDED}, version {body['version']} → "
                           f"{body['version'] + 1}")

    def retrievable(run: Run) -> str:
        body, _ = filed(run)
        stored = get(run, subject(run).id, BOARD)
        found = [f for f in stored["filings"] if f["accession_number"] == body["accession_number"]]
        if len(found) != 1:
            raise Fail(f"GET as Board lists {len(found)} filings with accession_number "
                       f"{body['accession_number']}, not 1")
        problems = [f"{key} reads back as {found[0][key]!r}, not {body[key]!r}"
                    for key in ("form_type", "filing_date") if found[0][key] != body[key]]
        return problems_or(problems, "GET as Board",
                           f"The filing reads back with form_type {body['form_type']}, "
                           f"filing_date {body['filing_date']} and accession_number "
                           f"{body['accession_number']}")

    return [
        *((f"AC-14-missing-{field}", missing(field))
          for field in ("form_type", "filing_date", "accession_number")),
        ("AC-14-filing-recorded", recorded),
        ("AC-14-filing-retrievable", retrievable),
    ]


# AC-12 (REQ-11): warning with 1 business day or less left, overdue after the due date, and
# neither once the filing is recorded. The clock moves through test_now.

AC12_DAY = date(2026, 10, 5)  # discovery and determination, a Monday
AC12_DUE = date(2026, 10, 9)


def ac12() -> list[Case]:
    def subject(run: Run) -> tuple[Tracked, dict[str, Any]]:
        def build() -> tuple[Tracked, dict[str, Any]]:
            inc = create(run, 0, discovery=AC12_DAY)
            import_items(run, inc, "ticketing", [ticket(16)])
            return inc, determine(run, inc, day=AC12_DAY)
        return run.once("ac12", build)

    def due_date(run: Run) -> str:
        due = expected_due(AC12_DAY, AC12_DUE)
        incident = subject(run)[1]
        if incident.get("due_date") != due.isoformat():
            raise Fail(f"due_date is {incident.get('due_date')!r}, not {due} (determination "
                       f"{AC12_DAY} + 4 business days)")
        return (f"due_date {due}: determination {AC12_DAY} + 4 business days, as the tester's "
                "calendar gives")

    def flags_on(day: date, expected: set[str], left: str):
        def case(run: Run) -> str:
            inc = subject(run)[0]
            with clock_at(run, day):
                incident = get(run, inc.id)
            return problems_or(flag_problems(f"GET with test_now {day}", incident, expected),
                               f"Due {AC12_DUE}, {left}",
                               f"{describe(expected).capitalize()} on {day} ({left})")
        return case

    def filing_clears(run: Run) -> str:
        inc = subject(run)[0]
        with clock_at(run, AC12_DUE + timedelta(days=1)):
            # Otherwise a system without flags would pass: there'd be nothing to clear.
            if problems := flag_problems("GET before the filing", get(run, inc.id), {"overdue"}):
                raise Fail(f"{problems[0]}, so the filing has nothing to clear")
            incident = file(run, inc, FORM_8K, day=AC12_DUE)
            stored = get(run, inc.id)
        problems = (flag_problems("The filing's response", incident, set())
                    + flag_problems("GET after it", stored, set()))
        return problems_or(problems, "Overdue incident's filing",
                           f"Overdue on {AC12_DUE + timedelta(days=1)}; after an 8-K Item 1.05 "
                           f"filing dated {AC12_DUE}, 201 and neither warning nor overdue")

    return [
        ("AC-12-due-date-4-business-days", due_date),
        ("AC-12-no-flag-2-business-days-left",
         flags_on(date(2026, 10, 7), set(), "2 business days left")),
        ("AC-12-warning-1-business-day-left",
         flags_on(date(2026, 10, 8), {"warning"}, "1 business day left")),
        ("AC-12-warning-on-due-date", flags_on(AC12_DUE, {"warning"}, "the due date")),
        ("AC-12-overdue-after-due-date",
         flags_on(AC12_DUE + timedelta(days=1), {"overdue"}, "the day after")),
        ("AC-12-filing-clears-flags", filing_clears),
    ]


# AC-13 (REQ-12): a foreign private issuer files a 6-K, with no 4-business-day clock, and
# records its home-country disclosure date. Registrant type is fixed per session, so the cases
# act in a second session they prepare as FPI.

FPI_DETERMINED = date(2026, 10, 4)
FPI_LATE = date(2026, 11, 30)


def ac13() -> list[Case]:
    def subject(run: Run) -> tuple[Run, Tracked, dict[str, Any]]:
        def build() -> tuple[Run, Tracked, dict[str, Any]]:
            fpi = run.prepare(f"{run.session_id}-fpi",
                              {"registrant_type": "FPI", "llm_base_url": failure_stub(run)})
            inc = create(fpi, 8)
            import_items(fpi, inc, "ticketing", [ticket(17)])
            return fpi, inc, determine(fpi, inc, day=FPI_DETERMINED)
        return run.once("ac13", build)

    def six_k(run: Run) -> str:
        incident = subject(run)[2]
        problems = []
        if incident.get("required_form") != FORM_6K:
            problems.append(f"required_form is {incident.get('required_form')!r}, not '6-K'")
        if incident.get("due_date") is not None:
            problems.append(f"due_date is {incident['due_date']}, not null")
        return problems_or(problems, "FPI Material determination",
                           "required_form 6-K, due_date null")

    def no_flags(run: Run) -> str:
        fpi, inc, _ = subject(run)
        with clock_at(fpi, FPI_LATE):
            incident = get(fpi, inc.id)
        problems = flag_problems(f"GET with test_now {FPI_LATE}", incident, set())
        if incident.get("due_date") is not None:
            problems.append(f"due_date is {incident['due_date']}, not null")
        return problems_or(problems, "FPI incident",
                           f"Neither warning nor overdue on {FPI_LATE}, well past "
                           f"{add_business_days(FPI_DETERMINED)} (determination + 4 business days)")

    def home_date_required(run: Run) -> str:
        fpi, inc, _ = subject(run)
        return rejected(fpi, "POST", FILINGS, APPROVER, filing_body(fpi, inc, FORM_6K), 400,
                        "VALIDATION_ERROR", "home_disclosure_date", incident_id=inc.id)

    def home_date_stored(run: Run) -> str:
        fpi, inc, _ = subject(run)
        home = FPI_DETERMINED.isoformat()
        body = filing_body(fpi, inc, FORM_6K, home_disclosure_date=home)
        incident = expect(fpi.call("POST", FILINGS, APPROVER, json=body, incident_id=inc.id), 201)
        inc.update(incident)
        stored = get(fpi, inc.id, BOARD)
        found = [f for f in stored["filings"] if f["accession_number"] == body["accession_number"]]
        problems = []
        if incident["state"] != RECORDED:
            problems.append(f"state is {incident['state']}, not {RECORDED}")
        if len(found) != 1:
            problems.append(f"GET as Board lists {len(found)} filings with accession_number "
                            f"{body['accession_number']}, not 1")
        elif found[0].get("home_disclosure_date") != home:
            problems.append(f"home_disclosure_date reads back as "
                            f"{found[0].get('home_disclosure_date')!r}, not {home!r}")
        return problems_or(problems, "6-K filing",
                           f"201; state {RECORDED}, home_disclosure_date {home} read back as "
                           "Board")

    return [
        ("AC-13-material-requires-6-K", six_k),
        ("AC-13-no-warning-or-overdue", no_flags),
        ("AC-13-6-K-missing-home_disclosure_date", home_date_required),
        ("AC-13-6-K-stores-home_disclosure_date", home_date_stored),
    ]


# AC-15 (REQ-14): information marked available moves a recorded disclosure to AmendmentPending,
# with an 8-K/A due 4 business days later.

AC15_DAY = date(2026, 11, 9)  # a Monday; Veterans Day is Wednesday 2026-11-11
AC15_DUE = date(2026, 11, 16)


def ac15() -> list[Case]:
    def amended(run: Run) -> tuple[int, dict[str, Any]]:
        def build() -> tuple[int, dict[str, Any]]:
            inc = incident_in(run, RECORDED, 9)
            version = inc.version
            # info_available_date can't be after today, so today moves to it.
            with clock_at(run, AC15_DAY):
                incident = move(run, inc, AMENDMENT, available=AC15_DAY)
            return version, incident
        return run.once("ac15", build)

    def pending(run: Run) -> str:
        version, incident = amended(run)
        problems = []
        if incident["state"] != AMENDMENT:
            problems.append(f"state is {incident['state']}, not {AMENDMENT}")
        if incident["version"] != version + 1:
            problems.append(f"version is {incident['version']}, not {version + 1}")
        return problems_or(problems, f"Transition with info_available_date {AC15_DAY}",
                           f"200; state {AMENDMENT} at version {version + 1}")

    def form(run: Run) -> str:
        incident = amended(run)[1]
        if incident.get("required_form") != FORM_8KA:
            raise Fail(f"required_form is {incident.get('required_form')!r}, not '8-K/A'")
        return "required_form 8-K/A"

    def due_date(run: Run) -> str:
        due = expected_due(AC15_DAY, AC15_DUE)
        incident = amended(run)[1]
        if incident.get("due_date") != due.isoformat():
            raise Fail(f"due_date is {incident.get('due_date')!r}, not {due} "
                       f"(info_available_date {AC15_DAY} + 4 business days, skipping Veterans Day)")
        return (f"due_date {due}: info_available_date {AC15_DAY} + 4 business days, skipping "
                "Veterans Day, as the tester's calendar gives")

    return [
        ("AC-15-amendment-pending", pending),
        ("AC-15-amendment-requires-8-K-A", form),
        ("AC-15-due-date-skips-Veterans-Day", due_date),
    ]


# AC-06 (REQ-06): all 49 state pairs.


def ac06() -> list[Case]:
    # Fixture incident per source state; a source's allowed moves run after its refused ones.
    sources = {UA: 0, DM: 2, DELAYED: 3, DNM: 4, CLOSED: 5, RECORDED: 6, AMENDMENT: 7}

    def subject(run: Run, source: str) -> Tracked:
        return run.once(f"ac06-{source}", lambda: incident_in(run, source, sources[source]))

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
            if to == AMENDMENT and source not in FILED and reply.status == 400:
                # With no filing, elements can't be a subset of the latest filing's
                # unavailable_elements, so a 400 on elements also rejects the move.
                expect(reply, 400, "VALIDATION_ERROR", "elements")
                answer = "400 VALIDATION_ERROR on elements"
            else:
                expect(reply, 409, "INVALID_TRANSITION")
                answer = "409 INVALID_TRANSITION"
            if moved:
                raise Fail(f"{answer} returned, but the incident is now {after['state']} at "
                           f"version {after['version']}")
            return f"{answer}; still {source} at version {inc.version}"
        return case

    def allowed(source: str, to: str):
        def case(run: Run) -> str:
            inc = subject(run, source)
            run.forget(f"ac06-{source}")
            before = get(run, inc.id)
            version = inc.version
            if (source, to) in DETERMINATION_MOVES:
                route, status = "/determination", 200
                incident = determine(run, inc, "Material" if to == DM else "NotMaterial")
            elif (source, to) in FILING_MOVES:
                route, status = f"/filings ({FILING_FORMS[source]})", 201
                incident = file(run, inc, FILING_FORMS[source])
            else:
                route, status = "/transitions", 200
                incident = move(run, inc, to)
            problems = []
            if incident["state"] != to:
                problems.append(f"state is {incident['state']}")
            if incident["version"] != version + 1:
                problems.append(f"version went from {version} to {incident['version']}")
            if route != "/determination" and incident.get("determination") != before.get(
                    "determination"):
                problems.append(f"{route.split()[0]} changed the determination")
            return problems_or(problems, f"{name(source)} → {name(to)}",
                               f"{status} through {route}; {to} at version {version + 1}")
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

    def unknown_incident(run: Run) -> str:
        expect(run.call("POST", TRANSITIONS, APPROVER, json={"to": CLOSED, "version": 1},
                        incident_id=UNKNOWN_ID), 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    allowed_pairs = DETERMINATION_MOVES | TRANSITION_MOVES | FILING_MOVES
    cases: list[Case] = []
    for source in sources:
        targets = [to for to in STATES if (source, to) not in allowed_pairs]
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
        # A move that only /filings performs is refused through /transitions.
        cases += [(f"AC-06-{name(source)}-to-{name(to)}-via-transitions", refused(source, to))
                  for to in STATES if (source, to) in FILING_MOVES]
        cases += [(f"AC-06-{name(source)}-to-{name(to)}", allowed(source, to))
                  for to in STATES if (source, to) in allowed_pairs]
    cases.append(("AC-06-unknown-incident", unknown_incident))
    return cases


# AC-09 (a) (REQ-08): no route but the determination one sets a materiality value.


def ac09() -> list[Case]:
    def case_for(n: int):
        def case(run: Run) -> str:
            inc = create(run, n)
            import_items(run, inc, "ticketing", [ticket(2 * n), ticket(2 * n + 1)])
            evidence_id = inc.evidence[0]
            calls = [
                ("POST", DRAFTS, APPROVER, {}),
                *(("POST", TRANSITIONS, APPROVER, {"json": transition_body(inc, to)})
                  for to in STATES),
                ("POST", FILINGS, APPROVER, {"json": filing_body(run, inc, FORM_8K)}),
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

    def material_draft(run: Run) -> tuple[dict, Reply, dict]:
        def build() -> tuple[dict, Reply, dict]:
            inc = drafting_subject(run)
            before = get(run, inc.id)
            reply = draft(run, inc, model_reply(
                nature=element("An unauthorized party encrypted files on a shared drive.",
                               inc.evidence[:1])))
            return before, reply, get(run, inc.id)
        return run.once("ac09-c", build)

    def no_materiality(run: Run) -> str:
        before, reply, after = material_draft(run)
        body = expect(reply, 201)
        problems = [f"the draft has a materiality field at {f}" for f in materiality_fields(body)]
        problems += [f"the draft changed {key} from {before.get(key)!r} to {after.get(key)!r}"
                     for key in ("state", "version", "determination")
                     if after.get(key) != before.get(key)]
        return problems_or(problems, reply.label,
                           "201 draft with no materiality field; the recorded determination, "
                           "state and version unchanged")

    def materiality_dropped(run: Run) -> str:
        inc = drafting_subject(run)
        content = {**model_reply(nature=element("Files on a shared drive were encrypted.",
                                                inc.evidence[:1])), "materiality": "Material"}
        return not_passed_on(run, inc, content, "the model's materiality field")

    return [
        *((f"AC-09-a-incident-{n + 1:02d}", case_for(n)) for n in range(10)),
        ("AC-09-c-draft-has-no-materiality-field", no_materiality),
        ("AC-09-c-model-materiality-not-passed-on", materiality_dropped),
    ]


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
                               "5 s of the call")
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


# AC-04 (REQ-04): three incidents link into a group, one determination applies to all of them,
# a second link creates no duplicate, and an incident can't link to itself.


def ac04() -> list[Case]:
    def members(run: Run) -> list[Tracked]:
        def build() -> list[Tracked]:
            incidents = [create(run, n) for n in (0, 1, 2)]
            import_items(run, incidents[0], "ticketing", [ticket(19)])
            return incidents
        return run.once("ac04", build)

    def grouped(run: Run) -> tuple[dict, list[dict], list[int]]:
        def build() -> tuple[dict, list[dict], list[int]]:
            incidents = members(run)
            versions = [inc.version for inc in incidents]
            group = expect(run.call("POST", GROUPS, RESPONDER,
                                    json={"incident_ids": [inc.id for inc in incidents]}), 201)
            read = [get(run, inc.id) for inc in incidents]
            for inc, incident in zip(incidents, read):
                inc.update(incident)
            return group, read, versions
        return run.once("ac04-group", build)

    def ungrouped(run: Run, key: str, n: int) -> Tracked:
        return run.once(key, lambda: create(run, n))

    def group_created(run: Run) -> str:
        group, read, versions = grouped(run)
        ids = [inc.id for inc in members(run)]
        problems = []
        if sorted(group["incident_ids"]) != sorted(ids):
            problems.append(f"the group lists {group['incident_ids']}, not {ids}")
        for incident, version in zip(read, versions):
            if incident.get("group_id") != group["group_id"]:
                problems.append(f"{incident['incident_id']} has group_id "
                                f"{incident.get('group_id')!r}")
            if (incident["state"], incident["version"]) != (UA, version + 1):
                problems.append(f"{incident['incident_id']} is {incident['state']} at version "
                                f"{incident['version']}, not {UA} at {version + 1}")
        return problems_or(problems, "POST /groups",
                           "201; all three carry the group_id, still UnderAssessment, version +1")

    def relinked(run: Run) -> str:
        grouped(run)
        first, second, _ = members(run)
        message = rejected(run, "POST", GROUPS, APPROVER, {"incident_ids": [first.id, second.id]},
                           409, "CONFLICT")
        return f"{message}: linking grouped incidents again creates no duplicate"

    def refused(body, status: int, error: str, field: str | None = None, actor=RESPONDER):
        def case(run: Run) -> str:
            return rejected(run, "POST", GROUPS, actor, body(run), status, error, field)
        return case

    def single(run: Run) -> str:
        return ungrouped(run, "ac04-single", 3).id

    def pair(run: Run) -> list[str]:
        return [single(run), ungrouped(run, "ac04-second", 4).id]

    def with_material(run: Run) -> list[str]:
        return [single(run), run.once("ac04-dm", lambda: incident_in(run, DM, 5)).id]

    def determined_together(run: Run) -> str:
        grouped(run)
        incidents = members(run)
        # No member can be determined before its discovery, so the date is the latest one's.
        day = max(inc.discovery for inc in incidents)
        decision = determine(run, incidents[0], day=day)["determination"]
        problems = []
        for inc in incidents[1:]:
            incident = get(run, inc.id)
            stored = incident.get("determination") or {}
            if incident["state"] != DM:
                problems.append(f"{inc.id} is {incident['state']}, not {DM}")
            if (stored.get("decision"), stored.get("determination_date")) != (
                    decision["decision"], decision["determination_date"]):
                problems.append(f"{inc.id} has determination {incident.get('determination')}")
        return problems_or(problems, "Determination on one member",
                           f"All three {DM} with the same Material determination")

    return [
        ("AC-04-three-incidents-grouped", group_created),
        ("AC-04-second-link-no-duplicate", relinked),
        ("AC-04-self-link-rejected",
         refused(lambda run: {"incident_ids": [single(run), single(run)]}, 400,
                 "VALIDATION_ERROR", "incident_ids")),
        ("AC-04-one-incident-rejected",
         refused(lambda run: {"incident_ids": [single(run)]}, 400, "VALIDATION_ERROR",
                 "incident_ids")),
        ("AC-04-group-as-Board-forbidden",
         refused(lambda run: {"incident_ids": pair(run)}, 403, "FORBIDDEN_ROLE", actor=BOARD)),
        ("AC-04-unknown-incident",
         refused(lambda run: {"incident_ids": [single(run), UNKNOWN_ID]}, 404, "NOT_FOUND")),
        ("AC-04-member-not-under-assessment",
         refused(lambda run: {"incident_ids": with_material(run)}, 409, "CONFLICT")),
        ("AC-04-group-determination-on-all", determined_together),
    ]


# AC-05 (REQ-05): an evidence item stores all five impact categories; an undefined one is
# rejected with the item unchanged.


def ac05() -> list[Case]:
    def subject(run: Run) -> Tracked:
        def build() -> Tracked:
            inc = create(run, 6)
            import_items(run, inc, "ticketing", [ticket(3)])
            return inc
        return run.once("ac05", build)

    def path(run: Run, evidence_id: str | None = None) -> dict[str, str]:
        inc = subject(run)
        return {"incident_id": inc.id, "evidence_id": evidence_id or inc.evidence[0]}

    def tag(run: Run, categories: list[str], actor: Actor = RESPONDER) -> dict[str, Any]:
        reply = run.call("PATCH", EVIDENCE, actor, json={"impact_categories": categories},
                         **path(run))
        return expect(reply, 200)

    def stored_categories(run: Run) -> list[str]:
        inc = subject(run)
        found = [e for e in get(run, inc.id, BOARD)["evidence"]
                 if e["evidence_id"] == inc.evidence[0]]
        if len(found) != 1:
            raise Fail(f"GET as Board lists {len(found)} items with the tagged evidence_id")
        return found[0]["impact_categories"]

    def tagged(run: Run) -> dict[str, Any]:
        return run.once("ac05-tagged", lambda: tag(run, IMPACT_CATEGORIES))

    def all_five(run: Run) -> str:
        item = tagged(run)
        problems = []
        if sorted(item["impact_categories"]) != sorted(IMPACT_CATEGORIES):
            problems.append(f"the response has {item['impact_categories']}")
        if sorted(stored := stored_categories(run)) != sorted(IMPACT_CATEGORIES):
            problems.append(f"GET as Board reads {stored}")
        return problems_or(problems, "PATCH with all five categories",
                           f"200; all five read back: {', '.join(IMPACT_CATEGORIES)}")

    def bad(categories: list[str], status: int = 400, error: str = "VALIDATION_ERROR",
            field: str | None = "impact_categories", actor: Actor = RESPONDER):
        def case(run: Run) -> str:
            tagged(run)
            return rejected(run, "PATCH", EVIDENCE, actor, {"impact_categories": categories},
                            status, error, field, **path(run))
        return case

    def unknown_evidence(run: Run) -> str:
        reply = run.call("PATCH", EVIDENCE, RESPONDER, json={"impact_categories": ["financial"]},
                         **path(run, UNKNOWN_ID))
        expect(reply, 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    def replaced(run: Run) -> str:
        tagged(run)
        item = tag(run, ["financial"], APPROVER)
        problems = []
        if item["impact_categories"] != ["financial"]:
            problems.append(f"the response has {item['impact_categories']}")
        if (stored := stored_categories(run)) != ["financial"]:
            problems.append(f"GET as Board reads {stored}")
        return problems_or(problems, "PATCH as DisclosureApprover",
                           "200; the five categories replaced by financial alone")

    return [
        ("AC-05-five-categories-stored", all_five),
        ("AC-05-misc-rejected", bad(["misc"])),
        ("AC-05-misc-among-valid-rejected", bad(["operational", "misc"])),
        ("AC-05-repeated-category-rejected", bad(["operational", "operational"])),
        ("AC-05-tag-as-Board-forbidden",
         bad(["financial"], 403, "FORBIDDEN_ROLE", None, actor=BOARD)),
        ("AC-05-unknown-evidence", unknown_evidence),
        ("AC-05-tags-replaced-by-approver", replaced),
    ]


# AC-19 (REQ-18): a NotMaterial incident marked for voluntary disclosure is designated 8-K
# Item 8.01; a Material one stays Item 1.05.


def ac19() -> list[Case]:
    def evidenced(run: Run, key: str, n: int) -> Tracked:
        def build() -> Tracked:
            inc = create(run, n)
            import_items(run, inc, "ticketing", [ticket(n + 1)])
            return inc
        return run.once(key, build)

    def voluntary(run: Run) -> tuple[Tracked, dict, dict]:
        def build() -> tuple[Tracked, dict, dict]:
            inc = evidenced(run, "ac19-a-incident", 7)
            body = {**determination(inc, "NotMaterial"), "voluntary_disclosure": True}
            incident = expect(run.call("POST", DETERMINATION, APPROVER, json=body,
                                       incident_id=inc.id), 200)
            inc.update(incident)
            return inc, body, incident
        return run.once("ac19-a", build)

    def item_801(incident: dict[str, Any]) -> list[str]:
        problems = []
        if incident.get("required_form") != FORM_801:
            problems.append(f"required_form is {incident.get('required_form')!r}, not "
                            f"{FORM_801!r}")
        if incident.get("due_date") is not None:
            problems.append(f"due_date is {incident['due_date']}, not null")
        if (incident.get("determination") or {}).get("voluntary_disclosure") is not True:
            problems.append("the determination doesn't record voluntary_disclosure true")
        return problems

    def designated(run: Run) -> str:
        _, body, incident = voluntary(run)
        problems = determination_problems(body, incident, DNM) + item_801(incident)
        return problems_or(problems, "NotMaterial with voluntary_disclosure",
                           f"200; {DNM}, required_form {FORM_801}, due_date null")

    def retrievable(run: Run) -> str:
        inc = voluntary(run)[0]
        return problems_or(item_801(get(run, inc.id, BOARD)), "GET as Board",
                           f"required_form {FORM_801} and voluntary_disclosure true read back")

    def stays_105(run: Run) -> str:
        voluntary(run)
        inc = run.once("ac19-b", lambda: material(run, 8, [ticket(9)]))
        incident = get(run, inc.id)
        if incident.get("required_form") != FORM_8K:
            raise Fail(f"required_form is {incident.get('required_form')!r}, not {FORM_8K!r}")
        return f"The Material incident stays {FORM_8K} after the other's voluntary disclosure"

    def bad(decision: str, value: Any):
        def case(run: Run) -> str:
            inc = evidenced(run, "ac19-undetermined", 9)
            body = {**determination(inc, decision), "voluntary_disclosure": value}
            return rejected(run, "POST", DETERMINATION, APPROVER, body, 400, "VALIDATION_ERROR",
                            "voluntary_disclosure", incident_id=inc.id)
        return case

    return [
        ("AC-19-voluntary-not-material-gives-8-01", designated),
        ("AC-19-voluntary-retrievable", retrievable),
        ("AC-19-material-stays-1-05", stays_105),
        ("AC-19-voluntary-with-Material-rejected", bad("Material", True)),
        ("AC-19-voluntary-not-boolean-rejected", bad("NotMaterial", "yes")),
    ]


# AC-16 (REQ-15): drafts of 10 Material incidents each have all four elements, with text or
# "Not yet determined". The model's replies come from the stub, so the checks are structural.

AC16_TEXTS = {
    "nature": "An unauthorized party encrypted files on a shared drive.",
    "scope": "About 120 employees lost access to three shared folders.",
    "timing": "The activity was detected and contained the day it began.",
    "impact": "Operations were interrupted for about two hours.",
}
# The drafts whose model leaves impact Not yet determined.
AC16_UNDETERMINED_IMPACT = {2, 5, 8}


def ac16_draft(run: Run, k: int) -> tuple[Tracked, dict[str, Any], dict[str, Any]]:
    """AC-16's kth incident with four tickets, the model's reply, and the 201 draft."""
    def build() -> tuple[Tracked, dict[str, Any], dict[str, Any]]:
        inc = material(run, k, [ticket(4 * k + j) for j in range(4)])
        content = model_reply(**{
            name: element(AC16_TEXTS[name], [inc.evidence[j]])
            for j, name in enumerate(ELEMENTS)
            if not (name == "impact" and k in AC16_UNDETERMINED_IMPACT)})
        return inc, content, expect(draft(run, inc, content), 201)
    return run.once(f"ac16-{k}", build)


def ac16() -> list[Case]:
    def structured(k: int):
        def case(run: Run) -> str:
            inc, content, body = ac16_draft(run, k)
            problems = structure_problems(body, inc)
            problems += [f"{name} reads {body['elements'][name]['text']!r}, not the model's "
                         f"{content[name]['text']!r}" for name in ELEMENTS
                         if body["elements"][name]["text"] != content[name]["text"]]
            undetermined = [n for n in ELEMENTS if content[n]["text"] == UNDETERMINED]
            return problems_or(problems, f"Draft {k + 1}",
                               f"201 Proposed {FORM_8K} draft with all four elements"
                               + (f", {undetermined[0]} {UNDETERMINED!r}" if undetermined else ""))
        return case

    def missing_impact(run: Run) -> str:
        inc = drafting_subject(run)
        content = model_reply(nature=element(AC16_TEXTS["nature"], inc.evidence[:1]))
        del content["impact"]
        return not_passed_on(run, inc, content, "a reply missing impact")

    def ua(run: Run) -> Tracked:
        return run.once("ac16-ua", lambda: incident_in(run, UA, 9))

    def refused(subject, actor: Actor, status: int, error: str):
        def case(run: Run) -> str:
            return rejected(run, "POST", DRAFTS, actor, NO_BODY, status, error,
                            incident_id=subject(run).id)
        return case

    def unknown_incident(run: Run) -> str:
        expect(run.call("POST", DRAFTS, APPROVER, incident_id=UNKNOWN_ID), 404, "NOT_FOUND")
        return "404 NOT_FOUND"

    return [
        *((f"AC-16-draft-{k + 1:02d}-has-four-elements", structured(k)) for k in range(10)),
        ("AC-16-reply-missing-element-not-passed-on", missing_impact),
        ("AC-16-draft-as-Responder-forbidden",
         refused(drafting_subject, RESPONDER, 403, "FORBIDDEN_ROLE")),
        ("AC-16-draft-under-assessment-rejected", refused(ua, APPROVER, 409, "NOT_MATERIAL")),
        ("AC-16-draft-unknown-incident", unknown_incident),
    ]


# AC-17 (REQ-16): every drafted statement cites the incident's own evidence, and the impact of
# 5 incidents without impact evidence is exactly "Not yet determined".


def ac17() -> list[Case]:
    def cited(k: int):
        def case(run: Run) -> str:
            inc, _, body = ac16_draft(run, k)
            count = sum(len(body["elements"][name]["citations"]) for name in ELEMENTS)
            return problems_or(citation_problems(body, inc), f"Draft {k + 1}",
                               f"{count} citations, all to this incident's evidence; every "
                               "drafted element cites at least one")
        return case

    def no_impact_draft(run: Run, k: int) -> tuple[Tracked, dict[str, Any]]:
        def build() -> tuple[Tracked, dict[str, Any]]:
            inc = material(run, k, [ticket(4 * k + j + 2) for j in range(3)])
            content = model_reply(**{name: element(AC16_TEXTS[name], [inc.evidence[j]])
                                     for j, name in enumerate(ELEMENTS[:3])})
            return inc, expect(draft(run, inc, content), 201)
        return run.once(f"ac17-b-{k}", build)

    def impact_undetermined(k: int):
        def case(run: Run) -> str:
            inc, body = no_impact_draft(run, k)
            impact = body["elements"]["impact"]
            problems = citation_problems(body, inc)
            if impact != element(UNDETERMINED, []):
                problems.insert(0, f"impact is {impact}")
            return problems_or(problems, f"Incident {k + 1} without impact evidence",
                               f"impact exactly {UNDETERMINED!r}, no citations or flags")
        return case

    def other(run: Run) -> Tracked:
        def build() -> Tracked:
            inc = create(run, 7)
            import_items(run, inc, "ticketing", [ticket(17)])
            return inc
        return run.once("ac17-other", build)

    def fault(citations, what: str):
        def case(run: Run) -> str:
            inc = drafting_subject(run)
            content = model_reply(nature=element(AC16_TEXTS["nature"], citations(run, inc)))
            return not_passed_on(run, inc, content, what)
        return case

    return [
        *((f"AC-17-a-draft-{k + 1:02d}-cites-own-evidence", cited(k)) for k in range(10)),
        *((f"AC-17-b-incident-{k + 1:02d}-impact-not-yet-determined", impact_undetermined(k))
          for k in range(5)),
        ("AC-17-a-other-incident-citation-not-passed-on",
         fault(lambda run, inc: other(run).evidence[:1],
               "a citation of another incident's evidence")),
        ("AC-17-a-unknown-citation-not-passed-on",
         fault(lambda run, inc: [UNKNOWN_ID], "a citation of an unknown evidence ID")),
        ("AC-17-a-uncited-text-not-passed-on",
         fault(lambda run, inc: [], "drafted text without citations")),
    ]


# AC-18 (REQ-17): of 20 labeled texts, at least 9 of the 10 with technical details are flagged
# and at most 2 of the 10 without. The stub drafts them four at a time, one per element, and a
# text counts as flagged when its element has a sensitive flag.

LABELED_TEXTS = "evaluation/sensitive_texts"


def labeled_texts() -> list[dict[str, Any]]:
    """The team's 20 labeled texts; anything but 10 detailed and 10 clean is a tester fault."""
    texts = _fixture(LABELED_TEXTS)["texts"]
    if len(texts) != 20 or sum(bool(t["expected_sensitive"]) for t in texts) != 10:
        raise RuntimeError(f"fixtures/{LABELED_TEXTS}.json must hold 10 detailed and 10 clean "
                           "texts")
    return texts


def ac18() -> list[Case]:
    texts = labeled_texts()

    def flagged(run: Run) -> dict[str, bool]:
        def build() -> dict[str, bool]:
            inc = drafting_subject(run)
            found: dict[str, bool] = {}
            for start in range(0, len(texts), len(ELEMENTS)):
                batch = dict(zip(ELEMENTS, texts[start:start + len(ELEMENTS)]))
                content = {name: element(t["text"], [] if t["text"] == UNDETERMINED
                                         else inc.evidence[:1]) for name, t in batch.items()}
                body = expect(draft(run, inc, content), 201)
                for name, t in batch.items():
                    item = body["elements"][name]
                    if item["text"] != t["text"]:
                        raise Fail(f"{name} came back as {item['text']!r}, not "
                                   f"{t['text_id']}'s {t['text']!r}")
                    found[t["text_id"]] = bool(item["sensitive_flags"])
            return found
        return run.once("ac18", build)

    def detailed(run: Run) -> str:
        found = flagged(run)
        ids = [t["text_id"] for t in texts if t["expected_sensitive"]]
        missed = [i for i in ids if not found[i]]
        if len(ids) - len(missed) < 9:
            raise Fail(f"{len(ids) - len(missed)}/10 detailed texts flagged, below 9; not "
                       f"flagged: {', '.join(missed)}")
        return (f"{len(ids) - len(missed)}/10 detailed texts flagged (at least 9)"
                + (f"; not flagged: {', '.join(missed)}" if missed else ""))

    def clean(run: Run) -> str:
        found = flagged(run)
        wrong = [t["text_id"] for t in texts
                 if not t["expected_sensitive"] and found[t["text_id"]]]
        if len(wrong) > 2:
            raise Fail(f"{len(wrong)}/10 clean texts flagged, above 2: {', '.join(wrong)}")
        return (f"{len(wrong)}/10 clean texts flagged (at most 2)"
                + (f": {', '.join(wrong)}" if wrong else ""))

    return [
        ("AC-18-detailed-texts-flagged", detailed),
        ("AC-18-clean-texts-not-flagged", clean),
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
    return [*ac21(), *ac02(), *ac03(), *ac07(), *ac08(), *ac20(), *ac14(), *ac12(), *ac13(),
            *ac15(), *ac06(), *ac09(), *ac23(), *ac04(), *ac05(), *ac19(), *ac16(), *ac17(),
            *ac18(), *ac01()]
