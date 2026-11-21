"""app/services/drafts.py's request_draft and app/services/llm.py, with a fake LLM and no network."""

import asyncio
import gzip
import json
import time
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, update

import app.db as db
import app.services.drafts as drafts
import app.services.llm as llm
from app.config import get_settings
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import Draft, FormType, Incident, State
from app.services.drafts import MAX_TEXT_LENGTH, request_draft, scan_sensitive
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    alert,
    audit_entries,
    in_session,
    new_session,
    set_state,
    table_counts,
    ticket,
)

LLM_URL = "https://llm.test/v1"
UNDETERMINED = {"text": "Not yet determined", "citations": [], "sensitive_flags": []}


@pytest.fixture
def session() -> str:
    """A session whose LLM URL the fake transport answers; the default .invalid one never resolves."""
    return new_session(llm_base_url=LLM_URL)


def make_material(incident_id: str, form: FormType = FormType.FORM_8K_ITEM_105) -> None:
    with transaction() as tx:
        tx.execute(update(Incident).where(Incident.incident_id == incident_id)
                   .values(state=State.DETERMINED_MATERIAL, required_form=form))


@pytest.fixture
def material(session) -> dict[str, Any]:
    """A DeterminedMaterial incident with a ticket and an alert: {incident_id, evidence_ids}."""
    created = create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"},
                              **in_session(RESPONDER, session))
    incident_id = created["incident_id"]
    tickets = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]},
                              **in_session(RESPONDER, session))
    alerts = import_evidence(incident_id, {"source": "siem", "items": [alert()]},
                             **in_session(RESPONDER, session))
    make_material(incident_id)
    ids = [item["evidence_id"] for item in tickets["evidence"] + alerts["evidence"]]
    return {"incident_id": incident_id, "evidence_ids": ids, "session_id": session}


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a handler as the LLM; returns the list of requests it received."""
    requests: list[httpx2.Request] = []

    def install(handler):
        def recorded(request):
            requests.append(request)
            return handler(request)
        monkeypatch.setattr(llm, "TRANSPORT", httpx2.MockTransport(recorded))
        return requests
    return install


@pytest.fixture
def fresh_settings():
    def clear():
        get_settings.cache_clear()
        db.get_engine.cache_clear()
        db._session_factory.cache_clear()
    clear()
    yield
    clear()


def elements(*citations: str, nature: str | dict[str, Any] = "Ransomware encrypted file servers.",
             ) -> dict[str, Any]:
    """A model reply: nature drafted (text with these citations, or a whole element), the rest
    undetermined."""
    if isinstance(nature, str):
        nature = {"text": nature, "citations": list(citations), "sensitive_flags": []}
    return {"nature": nature, **{name: dict(UNDETERMINED) for name in ("scope", "timing", "impact")}}


def reply(content: Any) -> httpx2.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "content": text}}]})


def draft_rows(incident_id: str) -> list[Draft]:
    with transaction() as tx:
        return list(tx.scalars(select(Draft).where(Draft.incident_id == incident_id)))


def request(incident: dict[str, Any], who=APPROVER) -> dict[str, Any]:
    return request_draft(incident["incident_id"], **in_session(who, incident["session_id"]))


def read(incident: dict[str, Any]) -> dict[str, Any]:
    """The incident as a Board member of its session reads it."""
    return get_incident(incident["incident_id"], **in_session(BOARD, incident["session_id"]))


def never_called(request):
    pytest.fail("The LLM must not be called")


def test_a_valid_reply_is_stored_as_a_proposed_advisory_draft(material, fake_llm):
    incident_id, evidence_ids = material["incident_id"], material["evidence_ids"]
    sent = fake_llm(lambda request: reply(elements(*evidence_ids)))
    before = get_incident(incident_id, **in_session(BOARD, material["session_id"]))
    trail = [entry.entry_id for entry in audit_entries(incident_id)]

    draft = request(material)

    assert draft["incident_id"] == incident_id
    assert draft["draft_id"].startswith("drf_")
    assert draft["form_type"] == "8-K Item 1.05"
    assert (draft["advisory"], draft["status"]) == (True, "Proposed")
    assert (draft["accepted_by"], draft["accepted_at"]) == (None, None)
    assert draft["elements"] == elements(*evidence_ids)
    # Stored as returned; the incident, its version and its audit trail are unchanged.
    [row] = draft_rows(incident_id)
    assert (row.draft_id, row.elements) == (draft["draft_id"], draft["elements"])
    assert get_incident(incident_id, **in_session(BOARD, material["session_id"])) == before
    assert [entry.entry_id for entry in audit_entries(incident_id)] == trail

    [call] = sent
    assert str(call.url) == f"{LLM_URL}/chat/completions"
    assert "authorization" not in call.headers
    payload = json.loads(call.content)
    prompt = json.loads(payload["messages"][1]["content"])
    assert prompt["incident"] == {"title": "Ransomware", "form_type": "8-K Item 1.05"}
    assert [item["evidence_id"] for item in prompt["evidence"]] == evidence_ids
    assert "model" not in payload


@pytest.mark.parametrize("state", [State.DISCLOSURE_DELAYED, State.AMENDMENT_PENDING])
def test_every_eligible_state_drafts(material, fake_llm, state):
    set_state(material["incident_id"], state)
    fake_llm(lambda request: reply(elements(material["evidence_ids"][0])))
    assert request(material)["status"] == "Proposed"


@pytest.mark.parametrize("opening", ["```json\n", "```JSON ", "```\n", "```"])
def test_a_reply_in_a_code_fence_is_accepted(material, fake_llm, opening):
    fenced = opening + json.dumps(elements(material["evidence_ids"][0])) + "\n```"
    fake_llm(lambda request: reply(fenced))
    assert request(material)["elements"]["nature"]["citations"] == [material["evidence_ids"][0]]


def test_a_long_fenced_reply_is_unwrapped_quickly(material, fake_llm):
    """A run of spaces inside a fence once took time growing with the square of its length:
    minutes for this reply. Its content isn't JSON, so it's a 503."""
    fake_llm(lambda request: reply("```x" + " " * 400_000 + "x```"))
    before, counts = read(material), table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        request(material)
    elapsed = time.monotonic() - started
    assert (raised.value.code, raised.value.field) == (ErrorCode.DRAFTING_UNAVAILABLE, None)
    assert elapsed < 2
    assert table_counts() == counts
    assert read(material) == before


def test_no_evidence_drafts_every_element_undetermined_without_the_llm(session, fake_llm):
    created = create_incident({"title": "Empty", "discovery_date": "2026-10-01"},
                              **in_session(RESPONDER, session))
    make_material(created["incident_id"], FormType.FORM_6K)
    sent = fake_llm(never_called)
    draft = request({"incident_id": created["incident_id"], "session_id": session})
    assert draft["form_type"] == "6-K"
    assert draft["elements"] == {name: UNDETERMINED for name in ("nature", "scope", "timing", "impact")}
    assert sent == []


def raise_(error):
    def handler(request):
        raise error
    return handler


@pytest.mark.parametrize("envelope", ["object", "array", "mixed_array"])
@pytest.mark.parametrize("status,body,daily", [
    (429, {"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        }],
    }]}}, True),
    (429, {"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        }],
    }]}}, False),
    (429, {"error": {"message": "Daily quota exceeded; private provider text"}}, False),
    (429, {"error": {"details": [None, {"violations": "invalid"}]}}, False),
    (429, ["unexpected error"], False),
    (429, "not JSON", False),
    (429, b"<html>Rate limited</html>", False),
    (429, {"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [None, {
            "quotaId": None,
        }],
    }]}}, False),
    (500, {"error": {"details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        }],
    }]}}, False),
])
def test_daily_quota_notice_requires_an_explicit_daily_429(
    material, fake_llm, status, body, daily, envelope,
):
    """Quota errors write nothing and expose only a fixed daily notice, never provider text."""
    if envelope == "array":
        body = [body] if not isinstance(body, bytes) else body
    elif envelope == "mixed_array" and not isinstance(body, bytes):
        body = [None, "unrelated", {}, body]
    fake_llm(lambda request: httpx2.Response(
        status, **({"content": body} if isinstance(body, bytes) else {"json": body}),
    ))
    before, counts = read(material), table_counts()
    with pytest.raises(TracerError) as raised:
        request(material)
    error = raised.value
    assert (error.code, error.field, error.message) == (
        ErrorCode.DRAFTING_UNAVAILABLE, None, "Drafting unavailable",
    )
    if daily:
        assert error.detail == (
            "The daily AI drafting limit has been reached. "
            "Try again after the provider's daily quota resets."
        )
        assert error.to_dict()["detail"] == error.detail
    else:
        assert error.detail is None
        assert "detail" not in error.to_dict()
    assert table_counts() == counts
    assert read(material) == before


FAILURES = {
    "timeout": raise_(httpx2.ReadTimeout("slow")),
    "connection refused": raise_(httpx2.ConnectError("refused")),
    "server error": lambda request: httpx2.Response(500, json={"error": "down"}),
    "redirect": lambda request: httpx2.Response(302, headers={"Location": "https://elsewhere.test/"}),
    "not a chat completion": lambda request: httpx2.Response(200, text="<html>"),
    "content not JSON": lambda request: reply("Here is your draft: nature..."),
    "content not an object": lambda request: reply([1, 2]),
    "unknown citation": lambda request: reply(elements("ev_not_ours")),
    "materiality key": lambda request: reply({**elements("EVIDENCE"), "materiality": "Material"}),
    "text without a citation": lambda request: reply(elements()),
    "missing element": lambda request: reply({"nature": dict(UNDETERMINED)}),
    "NUL in text": lambda request: reply(elements("EVIDENCE", nature="bad\x00text")),
    "flag without a reason": lambda request: reply(elements(nature={
        "text": "Host 10.0.0.1", "citations": ["EVIDENCE"],
        "sensitive_flags": [{"excerpt": "10.0.0.1"}]})),
    "text too long": lambda request: reply(elements("EVIDENCE", nature="x" * (MAX_TEXT_LENGTH + 1))),
    # Nested too deeply for json.loads, which raises RecursionError rather than ValueError.
    "deeply nested content": lambda request: reply("[" * 100_000 + "]" * 100_000),
    "deeply nested reply": lambda request: httpx2.Response(
        200, content=b'{"choices": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"),
}


@pytest.mark.parametrize("failure", FAILURES)
def test_provider_failures_are_503_and_store_nothing(material, fake_llm, failure):
    incident_id = material["incident_id"]
    handler = FAILURES[failure]

    def with_real_ids(request):
        # "EVIDENCE" stands for a real evidence ID, so only the named fault fails validation.
        response = handler(request)
        if response.status_code == 200 and b"EVIDENCE" in response.content:
            body = response.content.replace(b"EVIDENCE", material["evidence_ids"][0].encode())
            return httpx2.Response(200, content=body)
        return response

    fake_llm(with_real_ids)
    before = get_incident(incident_id, **in_session(BOARD, material["session_id"]))
    counts = table_counts()
    with pytest.raises(TracerError) as raised:
        request(material)
    error = raised.value
    assert (error.status, error.code, error.message, error.field) == (
        503, ErrorCode.DRAFTING_UNAVAILABLE, "Drafting unavailable", None)
    assert table_counts() == counts
    assert get_incident(incident_id, **in_session(BOARD, material["session_id"])) == before


def test_no_answer_within_the_deadline_is_503(material, fake_llm, monkeypatch):
    monkeypatch.setattr(llm, "TIMEOUT_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(5)
        return reply(elements(material["evidence_ids"][0]))

    fake_llm(slow)
    counts = table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        request(material)
    assert raised.value.code == ErrorCode.DRAFTING_UNAVAILABLE
    assert time.monotonic() - started < 0.9
    assert table_counts() == counts


def slow_context_load(monkeypatch, seconds: float) -> None:
    """Make request_draft's first database read take `seconds` longer."""
    load = drafts.load_draft_context

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return load(*args, **kwargs)

    monkeypatch.setattr(drafts, "load_draft_context", slow)


def test_a_slow_database_read_counts_against_the_deadline(material, fake_llm, monkeypatch):
    """The budget starts with the request, so a slow read leaves the call less time instead of
    pushing the 503 later."""
    monkeypatch.setattr(llm, "TIMEOUT_SECONDS", 0.8)
    slow_context_load(monkeypatch, 0.6)

    async def silent(request):
        await asyncio.sleep(5)
        return reply(elements(material["evidence_ids"][0]))

    fake_llm(silent)
    before, counts = read(material), table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        request(material)
    elapsed = time.monotonic() - started
    assert (raised.value.code, raised.value.field) == (ErrorCode.DRAFTING_UNAVAILABLE, None)
    # 0.8 s in all; a deadline started at the call would end at about 1.4 s.
    assert elapsed < 1.2
    assert table_counts() == counts
    assert read(material) == before


def test_a_read_that_uses_up_the_deadline_is_503_without_a_request(
    material, fake_llm, monkeypatch,
):
    monkeypatch.setattr(llm, "TIMEOUT_SECONDS", 0.2)
    slow_context_load(monkeypatch, 0.3)
    sent = fake_llm(never_called)
    before, counts = read(material), table_counts()
    with pytest.raises(TracerError) as raised:
        request(material)
    assert (raised.value.code, raised.value.field) == (ErrorCode.DRAFTING_UNAVAILABLE, None)
    assert sent == []
    assert table_counts() == counts
    assert read(material) == before


def test_a_reply_trickling_past_the_deadline_is_cut_off(material, fake_llm, monkeypatch):
    """A byte at a time never trips httpx2's per-read timeout. The deadline cancels the read
    and closes the response, rather than leaving it running after the 503."""
    monkeypatch.setattr(llm, "TIMEOUT_SECONDS", 0.3)
    closed = []

    async def trickle():
        try:
            while True:
                yield b" "
                await asyncio.sleep(0.02)
        finally:
            closed.append(True)

    fake_llm(lambda request: httpx2.Response(200, content=trickle()))
    counts = table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        request(material)
    assert raised.value.code == ErrorCode.DRAFTING_UNAVAILABLE
    assert time.monotonic() - started < 2
    assert closed == [True]
    assert table_counts() == counts


@pytest.mark.parametrize("llm_base_url", [
    None,  # the default, https://llm.invalid/v1
    # The appended /chat/completions would land after the query or fragment.
    f"{LLM_URL}/files?purpose=",
    f"{LLM_URL}#",
])
def test_an_unusable_base_url_is_503_without_a_request(fake_llm, llm_base_url):
    session = new_session(llm_base_url=llm_base_url) if llm_base_url else new_session()
    created = create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"},
                              **in_session(RESPONDER, session))
    import_evidence(created["incident_id"], {"source": "ticketing", "items": [ticket()]},
                    **in_session(RESPONDER, session))
    make_material(created["incident_id"])
    incident = {"incident_id": created["incident_id"], "session_id": session}
    sent = fake_llm(never_called)
    before, counts = read(incident), table_counts()
    with pytest.raises(TracerError) as raised:
        request(incident)
    assert (raised.value.code, raised.value.field) == (ErrorCode.DRAFTING_UNAVAILABLE, None)
    assert sent == []
    assert table_counts() == counts
    assert read(incident) == before


def test_a_compressed_reply_is_refused(material, fake_llm):
    """The request asks for no compression, and the size limit counts bytes as received, so a
    compressed reply, which httpx2 would expand a whole chunk at a time, is a 503."""
    incident_id = material["incident_id"]
    body = json.dumps({"choices": [{"message": {
        "content": json.dumps(elements(material["evidence_ids"][0]))}}]}).encode()
    sent = fake_llm(lambda request: httpx2.Response(
        200, content=gzip.compress(body), headers={"Content-Encoding": "gzip"}))
    before = get_incident(incident_id, **in_session(BOARD, material["session_id"]))
    counts = table_counts()
    with pytest.raises(TracerError) as raised:
        request(material)
    assert (raised.value.code, raised.value.field) == (ErrorCode.DRAFTING_UNAVAILABLE, None)
    assert table_counts() == counts
    assert get_incident(incident_id, **in_session(BOARD, material["session_id"])) == before
    assert sent[0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("who,state,code", [
    (BOARD, State.DETERMINED_MATERIAL, ErrorCode.FORBIDDEN_ROLE),
    (RESPONDER, State.DETERMINED_MATERIAL, ErrorCode.FORBIDDEN_ROLE),
    (APPROVER, State.UNDER_ASSESSMENT, ErrorCode.NOT_MATERIAL),
    (APPROVER, State.DETERMINED_NOT_MATERIAL, ErrorCode.NOT_MATERIAL),
    (APPROVER, State.DISCLOSURE_RECORDED, ErrorCode.INVALID_TRANSITION),
    (APPROVER, State.CLOSED, ErrorCode.INVALID_TRANSITION),
])
def test_client_errors_come_before_the_llm(material, fake_llm, who, state, code):
    set_state(material["incident_id"], state)
    sent = fake_llm(never_called)
    counts = table_counts()
    with pytest.raises(TracerError) as raised:
        request(material, who)
    assert raised.value.code == code
    assert sent == [] and table_counts() == counts


def test_unknown_session_and_incident_come_before_the_llm(material, fake_llm):
    sent = fake_llm(never_called)
    for incident_id, session_id in [(material["incident_id"], "pytest-missing"),
                                    ("inc_missing", material["session_id"]),
                                    (material["incident_id"], new_session(llm_base_url=LLM_URL))]:
        with pytest.raises(TracerError) as raised:
            request_draft(incident_id, **in_session(APPROVER, session_id))
        assert raised.value.code == ErrorCode.NOT_FOUND
    assert sent == []


@pytest.mark.parametrize("moved_to", [State.DISCLOSURE_RECORDED, State.DISCLOSURE_DELAYED])
def test_a_state_change_during_the_call_is_409(material, fake_llm, moved_to):
    """Whether or not the new state allows drafting, the text was written for the old one."""
    def moved_meanwhile(request):
        set_state(material["incident_id"], moved_to)
        return reply(elements(material["evidence_ids"][0]))

    fake_llm(moved_meanwhile)
    counts = table_counts()
    with pytest.raises(TracerError) as raised:
        request(material)
    assert (raised.value.code, raised.value.field) == (ErrorCode.INVALID_TRANSITION, None)
    assert table_counts() == counts
    assert draft_rows(material["incident_id"]) == []


def test_a_form_change_during_the_call_is_409(material, fake_llm):
    def amended_meanwhile(request):
        with transaction() as tx:
            tx.execute(update(Incident).where(Incident.incident_id == material["incident_id"])
                       .values(state=State.AMENDMENT_PENDING, required_form=FormType.FORM_8KA))
        return reply(elements(material["evidence_ids"][0]))

    fake_llm(amended_meanwhile)
    with pytest.raises(TracerError) as raised:
        request(material)
    assert raised.value.code == ErrorCode.INVALID_TRANSITION
    assert draft_rows(material["incident_id"]) == []


def test_sensitive_details_are_flagged(material, fake_llm):
    text = ("Attackers exploited CVE-2024-3400 on vpn01.corp.example.com (10.20.30.40), an "
            "unpatched server, then changed a firewall rule.")
    model_flags = [
        {"excerpt": "vpn01.corp.example.com", "reason": "internal hostname"},
        {"excerpt": "not in the text", "reason": "invented"},
    ]
    drafted = elements(nature={"text": text, "citations": [material["evidence_ids"][0]],
                               "sensitive_flags": model_flags})
    fake_llm(lambda request: reply(drafted))
    flags = request(material)["elements"]["nature"]["sensitive_flags"]
    assert flags == [
        {"excerpt": "vpn01.corp.example.com", "reason": "internal hostname"},
        {"excerpt": "CVE-2024-3400", "reason": "CVE"},
        {"excerpt": "10.20.30.40", "reason": "IP address"},
        {"excerpt": "unpatched server", "reason": "unpatched system"},
        {"excerpt": "firewall rule", "reason": "firewall rule"},
    ]


@pytest.mark.parametrize("text,expected", [
    ("Beacon to 2001:db8::7334 seen", [("2001:db8::7334", "IP address")]),
    ("Loopback ::1 only", [("::1", "IP address")]),
    ("Started at 12:30:45 UTC", []),
    ("Version 1.2.3.4.5 shipped", []),
    ("Unpatched hosts remain", [("Unpatched hosts", "unpatched system")]),
    ("Systems were unpatched.", [("unpatched", "unpatched system")]),
    ("Two firewall rules changed", [("firewall rules", "firewall rule")]),
    ("Customer data was encrypted", []),
    ("Ransom note README.txt and invoice.PDF dropped", []),
    ("Files renamed to payroll.xlsx.locked", []),
    ("Exfiltrated to files.attacker.net as dump.zip",
     [("files.attacker.net", "hostname")]),
    ("Beacon to update-check.io", [("update-check.io", "hostname")]),
    ("Not yet determined", []),
])
def test_scan_sensitive(text, expected):
    assert [(flag["excerpt"], flag["reason"]) for flag in scan_sensitive(text, [])] == expected


@pytest.mark.parametrize("unit", ["a.", "1.", "a1-b."])
def test_scanning_the_longest_text_is_quick(unit):
    """Dotted runs that never end in a valid last label once took time growing with the square
    of their length: about 1.5 s for this length, and minutes for a 1 MB reply."""
    text = (unit * MAX_TEXT_LENGTH)[:MAX_TEXT_LENGTH]
    started = time.monotonic()
    scan_sensitive(text, [])
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("trusted,sent", [
    ("https://llm.test/v1", True),
    ("HTTPS://LLM.test:443/v1/", True),
    ("https://llm.test/other", False),
    ("https://llm.test/v1/chat", False),
    ("https://llm.test", False),
    ("https://llm.test/v1?x=1", False),
    ("https://llm.test:8443/v1", False),
    ("http://llm.test/v1", False),
    ("https://other.test/v1", False),
    (None, False),
])
def test_the_api_key_goes_only_to_the_trusted_base_url(
    material, fake_llm, monkeypatch, fresh_settings, trusted, sent,
):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    if trusted:
        monkeypatch.setenv("LLM_TRUSTED_BASE_URL", trusted)
    else:
        monkeypatch.delenv("LLM_TRUSTED_BASE_URL", raising=False)
    calls = fake_llm(lambda request: reply(elements(material["evidence_ids"][0])))
    request(material)
    [call] = calls
    assert (call.headers.get("authorization") == "Bearer sk-test") is sent
    assert json.loads(call.content)["model"] == "test-model"
