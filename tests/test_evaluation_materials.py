"""Integrity checks for offline evaluation inputs and unfilled reviewer templates."""

import csv
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.services.drafts import scan_sensitive
from app.services.drafts import validate_draft_elements
from app.services import llm
from app.errors import TracerError

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "fixtures" / "evaluation"
TEXTS = json.loads((MATERIALS / "sensitive_texts.json").read_text())["texts"]
CASES = json.loads((MATERIALS / "draft_cases.json").read_text())["cases"]


def rows(name):
    """Read one prepared spreadsheet without converting empty observations to scores."""
    with (MATERIALS / name).open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_dataset_sizes_and_identity():
    """Keep the promised sample sizes balanced, stable and unique."""
    assert len(TEXTS) == 20
    assert len({item["text_id"] for item in TEXTS}) == 20
    assert len({item["text"] for item in TEXTS}) == 20
    assert sum(item["expected_sensitive"] for item in TEXTS) == 10
    assert len(CASES) == 10
    assert len({case["case_id"] for case in CASES}) == 10
    assert sum(case["registrant_type"] == "Domestic" for case in CASES) == 5
    assert sum(case["registrant_type"] == "FPI" for case in CASES) == 5


@pytest.mark.parametrize("item", TEXTS, ids=lambda item: item["text_id"])
def test_proposed_sensitive_labels_match_scanner(item):
    """Verify the fixed detection examples without claiming LLM reliability or human review."""
    flags = item["expected_flags"]
    assert bool(flags) == item["expected_sensitive"]
    assert item["rationale"]
    assert all(flag["excerpt"] in item["text"] for flag in flags)
    expected = {(flag["excerpt"], flag["category"]) for flag in flags}
    actual = {(flag["excerpt"], flag["reason"]) for flag in scan_sensitive(item["text"], [])}
    assert actual == expected


def test_scenarios_have_valid_requests_and_review_references():
    """Check real contract shapes and source references before a person imports the cases."""
    spec = yaml.safe_load((ROOT / "system-openapi.yaml").read_text())
    for case in CASES:
        for schema, body in [("CreateIncidentRequest", case["incident"]),
                             ("ImportRequest", case["evidence_import"])]:
            validator = Draft202012Validator(
                {"$ref": f"#/components/schemas/{schema}", "components": spec["components"]},
                format_checker=FormatChecker(),
            )
            validator.validate(body)
        tickets = case["evidence_import"]["items"]
        ids = {ticket["ticket_id"] for ticket in tickets}
        assert len(ids) == len(tickets) == 4
        assert case["required_form"] == ("6-K" if case["registrant_type"] == "FPI" else "8-K Item 1.05")
        key = case["reviewer_key"]
        assert set(key["evidence_by_element"]) == {"nature", "scope", "timing", "impact"}
        for element, refs in key["evidence_by_element"].items():
            assert set(refs) <= ids
            assert bool(refs) == (element not in key["expected_unknown_elements"])
        assert not key["materiality_conclusions_allowed"]


def test_scoring_templates_are_complete_and_unscored():
    """Retain one row per case and no fabricated observations or reviewer identities."""
    permitted_prefills = {
        "sensitive_review.csv": {"text_id", "text"},
        "draft_review.csv": {"case_id", "registrant_type", "required_form"},
        "statement_review.csv": {"case_id", "sample_slot"},
        "evidence_map.csv": {"case_id", "ticket_id"},
        "missing_impact_review.csv": {"case_id"},
    }
    for name, allowed in permitted_prefills.items():
        for row in rows(name):
            assert all(not value for key, value in row.items() if key not in allowed), name
    assert {r["text_id"] for r in rows("sensitive_review.csv")} == {t["text_id"] for t in TEXTS}
    assert len(rows("sensitive_review.csv")) == 20
    assert {r["case_id"] for r in rows("draft_review.csv")} == {c["case_id"] for c in CASES}
    assert len(rows("draft_review.csv")) == 10
    statements = rows("statement_review.csv")
    assert len(statements) == 40
    assert {(r["case_id"], r["sample_slot"]) for r in statements} == {
        (c["case_id"], str(slot)) for c in CASES for slot in range(1, 5)
    }
    mapping = rows("evidence_map.csv")
    assert len(mapping) == 40
    assert {(r["case_id"], r["ticket_id"]) for r in mapping} == {
        (c["case_id"], t["ticket_id"]) for c in CASES for t in c["evidence_import"]["items"]
    }
    assert all(row["status"] == "NOT_RUN" and not row["observed"] for row in rows("evaluation_summary.csv"))


def test_five_missing_impact_cases_and_review_slots():
    """Keep AC-17's five missing-impact incidents distinct from the unknown-scope case."""
    expected = {"DRAFT-01", "DRAFT-02", "DRAFT-03", "DRAFT-05", "DRAFT-06"}
    actual = {c["case_id"] for c in CASES if "impact" in c["reviewer_key"]["expected_unknown_elements"]}
    assert actual == expected
    sheet = rows("missing_impact_review.csv")
    assert len(sheet) == 5 and {r["case_id"] for r in sheet} == expected
    assert rows("attempt_log.csv") == []
    assert rows("adjudication.csv") == []
    summary = {r["measure"]: r["required"] for r in rows("evaluation_summary.csv")}
    assert summary["sensitive_texts_flagged"] == "at least 9 of 10"
    assert summary["clean_texts_flagged"] == "no more than 2 of 10"
    assert "all 5 cases" in summary["missing_impact_placeholder"]


def test_capture_hook_preserves_completion_rejected_by_validation(tmp_path, monkeypatch):
    """Retain the original bytes before a real validator rejects an unknown citation."""
    original_parse = llm._parse
    captured = tmp_path / "provider-body.bin"
    elements = {name: {"text": "Not yet determined", "citations": [], "sensitive_flags": []}
                for name in ("nature", "scope", "timing", "impact")}
    elements["nature"] = {"text": "An incident occurred.", "citations": ["unknown"], "sensitive_flags": []}
    raw = json.dumps({"choices": [{"message": {"content": json.dumps(elements)}}]}).encode()

    def capture_parse(body):
        captured.write_bytes(body)
        return original_parse(body)

    monkeypatch.setattr(llm, "_parse", capture_parse)
    with pytest.raises(TracerError):
        validate_draft_elements(llm._parse(raw), evidence_ids={"local-evidence-id"})
    assert captured.read_bytes() == raw
