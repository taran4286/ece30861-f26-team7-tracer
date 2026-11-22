"""The tester's LLM stub over HTTP, and our system's drafting pointed at it."""

import json
import time
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

import app.services.llm as llm
from app.db import transaction
from app.errors import ErrorCode, TracerError
from app.models import FormType, Incident, State
from app.services.drafts import request_draft
from app.services.evidence import import_evidence
from app.services.incidents import create_incident, get_incident
from server import app as server_app
from tester import stub
from tests.helpers import (
    APPROVER,
    BOARD,
    RESPONDER,
    change_session,
    in_session,
    new_session,
    table_counts,
    ticket,
)

client = TestClient(server_app)
TESTER = "http://testserver/tester"
UNDETERMINED = {"text": "Not yet determined", "citations": [], "sensitive_flags": []}


def completion(url: str) -> httpx2.Response:
    """POST a minimal chat completion request to a stub base URL, as a system would."""
    path = url.removeprefix("http://testserver") + "/chat/completions"
    return client.post(path, json={"messages": [{"role": "user", "content": "Draft it"}]})


def test_the_fail_stub_answers_500_at_once():
    started = time.monotonic()
    response = completion(stub.fail_url(TESTER))
    assert time.monotonic() - started < 1
    assert response.status_code == 500
    assert response.json() == {
        "error": {"message": "The tester's LLM stub always fails", "type": "server_error"}}


def test_the_silent_stub_waits_past_the_specs_60_seconds(monkeypatch):
    assert stub.SILENT_SECONDS > 62
    monkeypatch.setattr(stub, "SILENT_SECONDS", 0.3)
    started = time.monotonic()
    response = completion(stub.silent_url(TESTER))
    assert time.monotonic() - started >= 0.3
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "timeout"


@pytest.mark.parametrize("content", [
    json.dumps({"nature": UNDETERMINED}),
    "not JSON at all",
    "Ünïcödé, a quote \" and a slash /",
    "x" * stub.MAX_CONTENT_BYTES,
])
def test_the_reply_stub_returns_its_content_as_a_chat_completion(content):
    url = stub.reply_url(TESTER, content)
    assert url.startswith(f"{TESTER}/llm/reply/")
    response = completion(url)
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": content}
    assert body["choices"][0]["finish_reason"] == "stop"


def test_the_reply_stub_returns_empty_content_at_its_own_base_url():
    url = stub.reply_url(TESTER, "")
    assert url == f"{TESTER}/llm/reply"
    response = completion(url)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": ""}


def test_a_trailing_slash_on_the_testers_url_is_dropped():
    assert stub.reply_url(f"{TESTER}/", "x") == stub.reply_url(TESTER, "x")
    assert stub.fail_url(f"{TESTER}/") == f"{TESTER}/llm/fail"


@pytest.mark.parametrize("encoded", [
    "not+base64",          # + and / aren't in the base64url alphabet
    "a",                   # a length no encoding has
    "_w",                  # 0xFF, which isn't UTF-8
    "A" * (-(-stub.MAX_CONTENT_BYTES * 4 // 3) + 4),  # over the size limit
])
def test_the_reply_stub_rejects_a_path_that_isnt_its_encoding(encoded):
    response = client.post(f"/tester/llm/reply/{encoded}/chat/completions", json={})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_content_one_byte_over_the_limit_is_rejected():
    url = stub.reply_url(TESTER, "x" * (stub.MAX_CONTENT_BYTES + 1))
    assert completion(url).status_code == 400


@pytest.mark.parametrize("path", [
    "/tester/llm/fail/chat/completions", "/tester/llm/reply/eA/chat/completions",
    "/tester/llm/fail", "/tester/llm/other/chat/completions",
])
def test_other_methods_and_paths_get_the_testers_501(path):
    response = client.get(path)
    assert response.status_code == 501
    assert response.json()["source"] == "tester"


# --- Our system drafting against the stub ---------------------------------------


@pytest.fixture
def through_the_stub(monkeypatch):
    """Our system's LLM calls reach the deployed app in-process, so /tester/llm answers them."""
    monkeypatch.setattr(llm, "TRANSPORT", httpx2.ASGITransport(app=server_app))


def material_incident(llm_base_url: str) -> dict[str, Any]:
    """A DeterminedMaterial incident with one ticket, in a new session using `llm_base_url`."""
    session = new_session(llm_base_url=llm_base_url)
    created = create_incident({"title": "Ransomware", "discovery_date": "2026-10-01"},
                              **in_session(RESPONDER, session))
    incident_id = created["incident_id"]
    evidence = import_evidence(incident_id, {"source": "ticketing", "items": [ticket()]},
                               **in_session(RESPONDER, session))["evidence"]
    with transaction() as tx:
        tx.execute(update(Incident).where(Incident.incident_id == incident_id).values(
            state=State.DETERMINED_MATERIAL, required_form=FormType.FORM_8K_ITEM_105))
    return {"incident_id": incident_id, "session_id": session,
            "evidence_id": evidence[0]["evidence_id"]}


def draft(incident: dict[str, Any]) -> dict[str, Any]:
    return request_draft(incident["incident_id"], **in_session(APPROVER, incident["session_id"]))


def read(incident: dict[str, Any]) -> dict[str, Any]:
    """The incident as a Board member of its session reads it."""
    return get_incident(incident["incident_id"], **in_session(BOARD, incident["session_id"]))


def assert_unavailable(raised: pytest.ExceptionInfo, incident: dict[str, Any],
                       before: dict[str, Any], counts: dict[str, int]) -> None:
    """The 503 a failed draft request gives, with no field, no rows and the incident as it was."""
    error = raised.value
    assert (error.code, error.field, error.message) == (
        ErrorCode.DRAFTING_UNAVAILABLE, None, "Drafting unavailable")
    assert table_counts() == counts
    assert read(incident) == before


@pytest.mark.parametrize("url", [stub.fail_url(TESTER), stub.reply_url(TESTER, "not JSON"),
                                 stub.reply_url(TESTER, "")])
def test_a_failing_stub_makes_drafting_unavailable_within_5_seconds(through_the_stub, url):
    incident = material_incident(url)
    before, counts = read(incident), table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        draft(incident)
    assert time.monotonic() - started < 5
    assert_unavailable(raised, incident, before, counts)


def test_the_silent_stub_makes_drafting_unavailable_at_the_deadline(through_the_stub, monkeypatch):
    monkeypatch.setattr(llm, "TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(stub, "SILENT_SECONDS", 5)
    incident = material_incident(stub.silent_url(TESTER))
    before, counts = read(incident), table_counts()
    started = time.monotonic()
    with pytest.raises(TracerError) as raised:
        draft(incident)
    assert 0.5 <= time.monotonic() - started < 2
    assert_unavailable(raised, incident, before, counts)


def test_a_reply_stub_drafts_what_it_was_given(through_the_stub):
    # The URL needs the evidence ID, so the session points at the stub after the import.
    incident = material_incident(stub.fail_url(TESTER))
    cited = {"text": "Ransomware encrypted the file server at 192.0.2.10.",
             "citations": [incident["evidence_id"]], "sensitive_flags": []}
    elements = {"nature": cited, "scope": UNDETERMINED, "timing": UNDETERMINED,
                "impact": UNDETERMINED}
    change_session(incident["session_id"],
                   llm_base_url=stub.reply_url(TESTER, json.dumps(elements)))
    result = draft(incident)
    assert result["status"] == "Proposed"
    assert result["elements"]["nature"]["citations"] == [incident["evidence_id"]]
    assert result["elements"]["nature"]["sensitive_flags"] == [
        {"excerpt": "192.0.2.10", "reason": "IP address"}]
    assert {name: result["elements"][name] for name in ("scope", "timing", "impact")} == {
        name: UNDETERMINED for name in ("scope", "timing", "impact")}
