# Draft evaluation materials

These are synthetic preparation materials, not completed reviews or evidence that an LLM passed. All examples use fictional incidents, reserved example hostnames and documentation IP addresses. Vulnerability identifiers are invented and must not be interpreted as real vulnerability reports. The 20 texts have author-proposed labels; a second person should approve them before the evaluation is frozen.

## Files

| File | Use |
| --- | --- |
| `sensitive_texts.json` | Answer key for 20 texts: 10 with sensitive details, 10 without; exact excerpts, categories and rationales. Includes multiple-detail cases and filename/version false-positive cases. |
| `sensitive_review.csv` | One blank review row per text. Can be opened in a spreadsheet. |
| `draft_cases.json` | Ten independent synthetic incident scenarios, five Domestic and five FPI, with ticketing imports and reviewer expectations. Includes unknown scope/impact, uncertain exfiltration, recovery, and sensitive details. |
| `evidence_map.csv` | Forty blank mappings from source ticket IDs to evidence IDs assigned during import. |
| `draft_review.csv` | Ten blank rows for generation metadata, all-citation totals, contradiction checks, element coverage and other human judgments. |
| `statement_review.csv` | Forty blank slots: four sampled factual statements per generated draft. |
| `evaluation_summary.csv` | Required thresholds with results blank and status `NOT_RUN`. |
| `missing_impact_review.csv` | Five blank AC-17 rows for the exact impact placeholder, empty citations and empty sensitive flags. |
| `attempt_log.csv` | Header-only manifest; append every generation attempt and its captured provider/API output paths. |
| `adjudication.csv` | Header-only log for disagreements resolved by a third reviewer, with evidence-based rationale. |

The saved-plan requirements supplied in PR #57's human review specify AC-18 text-level acceptance: flag at least 9/10 sensitive texts and no more than 2/10 clean texts. AC-17 requires five incidents without impact evidence to return the exact missing-information placeholder. DRAFT-01, DRAFT-02, DRAFT-03, DRAFT-05 and DRAFT-06 supply those cases. The 10 drafts and 40 statements also support the reliability evaluation in the Phase 5 paragraphs.

## Run a draft evaluation

1. Copy the CSV templates into a new results directory; keep the templates blank. Record the dataset version, code commit, provider/model identifier, prompt commit and run date. Freeze prompts and labels before collecting the final run. Keep raw outputs from failed attempts as well as successes; do not silently choose the best draft from repeated runs.
2. Configure and verify the provider and capture procedure below **before** creating drafts. For each scenario, create an isolated session through `POST /test/setup` with its `registrant_type`, the fixture's `test_now`, and the working `llm_base_url`. Omitting the URL keeps `https://llm.invalid/v1` and causes drafting to fail. Then use `POST /incidents` and `POST /incidents/{id}/evidence/import`. Send `incident` as the create body and `evidence_import` as the import body. Use Responder identity headers and that session's `X-Test-Run` on project calls. Record each returned evidence ID against its ticket ID in `evidence_map.csv`.
3. As DisclosureApprover, submit the scenario's `determination`, adding `version` from the incident and `evidence_refs` containing the imported IDs. Then call `POST /incidents/{id}/drafts` and save the entire returned draft unchanged. Record the actual session, incident, draft and generation metadata in `draft_review.csv`. The server builds its own prompt; never send `reviewer_key`, proposed sensitive labels, or scoring sheets to the LLM. Do not use source ticket IDs as evidence citations.
4. **Two independent reviewers** each review the same frozen raw outputs using separate copies of the draft, statement, sensitive-text and missing-impact sheets. Neither sees the other's judgments before submission. A **third reviewer** resolves disagreements in `adjudication.csv`, naming the disputed item, original judgments, final judgment and evidence rationale; do not average labels or let one original reviewer resolve the dispute alone. Use the adjudicated values for final totals while retaining both original sheets. Keep raw model output separate from corrected/rejected text. The examples' human-supplied Material determinations enable drafting but must not be restated or decided by the LLM.
5. Audit **every citation occurrence in all four elements**, not only the 40 sampled statements. A valid citation is an evidence ID actually imported for that incident. A valid ID does not establish that the evidence supports the claim. Record total/valid counts in `draft_review.csv`; a draft with factual text but no citations fails. Retain the raw draft and mapping so the counts can be checked.
6. Select statements using the fixed rule below, then fill the 40 slots. Review the entire draft for contradictions and materiality conclusions, not just the selected statements. Complete each element's coverage, unknown-information handling, and sensitive-detail check. Use `yes`/`no` for these fields, leaving unknown results blank until reviewed. A correct `Not yet determined` satisfies coverage for an element whose evidence is missing.
7. Complete the summary using the formulas below. No pass/fail decision is valid until all 10 drafts, 40 statement slots and all citation audits are complete, with reviewer names, dates and retained outputs. A drafting failure is a failed generation to report, not permission to delete the case or claim a smaller denominator.

## Provider configuration

The team's selected configuration, supplied by the human reviewer on PR #57, is model `gemini-3.5-flash-lite` with base URL `https://generativelanguage.googleapis.com/v1beta/openai`. Before the run, confirm the deployed service uses that model and a working credential; the reviewer reported authentication already configured, but that is not a test result from these materials.

Server-side settings are `LLM_MODEL=gemini-3.5-flash-lite`, `LLM_TRUSTED_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai`, and a private `LLM_API_KEY`. Keep the actual key in the server environment, never in fixtures, worksheets, commits or captured headers. Redeploy after changing deployed environment variables. The session URL must match the trusted scheme/host/port/path for the service to send the key. The service appends `/chat/completions`; do not put that suffix in the base URL.

For example, prepare DRAFT-01 with this JSON (substitute a unique session ID for each scenario and its registrant type):

```json
{
  "session_id": "evaluation-DRAFT-01-run-001",
  "setup": {
    "registrant_type": "Domestic",
    "test_now": "2026-10-05T15:00:00Z",
    "llm_base_url": "https://generativelanguage.googleapis.com/v1beta/openai"
  }
}
```

## Capture each attempt before validation

An API 503 contains only `Drafting unavailable`; it does **not** contain a rejected completion. Before starting, arrange provider-side request/response logging or an operator-controlled capture proxy that retains the exact completion body before returning it to Tracer. Record its request ID and a unique `(case_id, attempt_id)` in `attempt_log.csv`. If using a proxy, configure its own upstream credentials privately and use its URL as the session's base; do not redirect the production API key to an untrusted URL. Record the actual model and prompt version used. Do not assume provider logging is enabled or retains raw completions: verify this on a disposable attempt first.

An alternative for a local service evaluation is a capture hook around `app.services.llm._parse`. `_parse` receives the raw successful HTTP body before JSON parsing and before `validate_draft_elements` rejects unknown citations. Use the existing API with its validators enabled; install this hook only in the local test process, not in production code:

```python
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.services import llm
from server import app

# Prepare the local DB/session/incident/evidence/determination first.
case_id, attempt_id = "DRAFT-01", "001"
attempt_dir = Path("/private/tmp/tracer-evaluation-results") / case_id / attempt_id
attempt_dir.mkdir(parents=True, exist_ok=False)  # Never overwrite an earlier attempt.
original_parse = llm._parse

def capture_parse(body):
    (attempt_dir / "provider-body.bin").write_bytes(body)
    return original_parse(body)  # Keep parsing and all service validation enabled.

with patch.object(llm, "_parse", capture_parse), TestClient(app) as client:
    response = client.post(
        f"/system/incidents/{incident_id}/drafts",
        headers={"X-User-Id": "evaluator", "X-User-Role": "DisclosureApprover",
                 "X-Test-Run": session_id},
    )
    (attempt_dir / "api-response.json").write_text(response.text)
```

Run one attempt at a time with this process-global hook and define `incident_id` and `session_id` from the local setup. It captures a completion even if parsing or citation validation rejects it. The hook does not capture non-2xx bodies, compressed/oversized replies, partial timeout bytes, or failures before `_parse`; use provider/proxy capture for those, or explicitly mark `capture_status=unavailable` with the reason and API response. Never invent a raw body or rerun the provider and present the new output as the original. A no-response failure has no completion to capture and is logged as such. A complete raw-output evaluation requires observable completions; unavailable captures make that part incomplete.

For each attempt, append the manifest row **including failures**, keep the provider body and API response separately, and link them by case, attempt, session and incident IDs. Record successful and rejected generations; do not score only the accepted drafts, which have already passed citation validation. Inspect captured raw elements for invalid citation IDs, unsupported prose and contradictions. If output is unparseable or absent, record the generation failure and do not claim the full quality evaluation passed. Raw artifacts and server logs can contain synthetic evidence; capture no authorization headers and use only these synthetic cases.

## Five missing-impact checks (AC-17)

For each of the five named cases, both reviewers fill `missing_impact_review.csv` from the actual API draft: `actual_impact_text` must be exactly `Not yet determined`, `citations_json` must be `[]`, and `sensitive_flags_json` must be `[]`. All five must pass after adjudication. Also inspect the raw provider completion to distinguish correct LLM behavior from any service transformation. A 503 or missing response does not satisfy the placeholder check. Do not count the additional unknown-scope case as a sixth missing-impact case.

## Statement selection and labels

Traverse `nature`, `scope`, `timing`, then `impact` in that order, taking the first four factual sentences as four slots per draft. If a sentence makes several claims, judge it supported only when **all** claims are supported. Copy each sentence verbatim, including its element's citation IDs, and quote the supporting or conflicting evidence. Do not count the exact `Not yet determined` placeholder as a factual statement. Record a shortage instead of inventing or duplicating statements; the 40-statement evaluation is then incomplete and must be rerun under a documented revised protocol.

Use exactly one judgment per statement:

- `supported`: all factual claims follow from the cited evidence with appropriate uncertainty.
- `unsupported`: the cited evidence does not establish a claim, even if the ID is valid. This includes invented costs, dates, affected totals, unsupported certainty or claims supported only by uncited evidence.
- `contradicted`: at least one claim conflicts with the evidence, such as saying production was compromised when logs establish staging-only impact. Also mark the whole draft as containing a contradiction.

For a draft-wide contradiction check, inspect claims outside the four samples too. A materiality conclusion includes assertions that the incident is material/nonmaterial or that disclosure is legally required; record these separately even when the human determination already exists.

## Sensitive-text review

Run each exact text through the service's sensitive-detail scanner, or submit it through a controlled provider stub returning the text as a draft element with valid local evidence citations. Keep this controlled detection experiment separate from real LLM generation. Capture the actual returned flags as JSON in `observed_flags_json`; do not copy the answer key into that field. Reviewers can first assess the texts blind, then compare with the frozen answer key.

Match a flag to a gold excerpt and category; each gold span counts at most once. Record matched spans as true positives, unmatched actual flags as false positives, and missed expected spans as false negatives. Judge semantically equivalent category wording consistently and document it. Labels are context-specific to these synthetic texts, not a general rule that every filename or version is safe.

## Score the results

| Measure | Calculation | Required result from supplied plan |
| --- | --- | --- |
| Citation validity | Sum valid citation occurrences / sum all citation occurrences, across every draft | 100%; zero citations is not a pass |
| Statement support | Number of `supported` judgments in the 40 fixed slots | At least 36/40 |
| Contradiction-free drafts | Number of all 10 drafts with `contains_contradiction=no` | At least 9/10 |
| Materiality conclusions | Number of drafts with `contains_materiality_conclusion=yes` | None; materiality remains human-controlled |
| Missing impact (AC-17) | Exact placeholder with empty citations/flags in the five named cases | 5/5 |
| Sensitive texts flagged (AC-18) | Positive texts with at least one observed sensitive flag | At least 9/10 |
| Clean texts flagged (AC-18) | Clean texts with any observed sensitive flag | No more than 2/10 |
| Sensitive-detail precision | TP / (TP + FP) across all 20 labeled texts | Supplemental span-level measure |
| Sensitive-detail recall | TP / (TP + FN) across all 20 labeled texts | Supplemental span-level measure |

Use `INCOMPLETE` for missing observations, `PASS` only for a fully measured criterion meeting its threshold, and `FAIL` for a completed measurement missing it. Undefined precision/recall is `N/A`, not 100%. Other coverage/placeholder/flag observations remain visible alongside these totals; don't hide them in the numerical averages.

If the reliability thresholds are missed, record the failures and the mitigation described in the supplied plan: replace affected elements with `Not yet determined`, and have the pod decide whether thresholds need revision. These materials do not implement automatic detection of unsupported prose or change the service's behavior. Retain original and mitigated outputs separately and evaluate any revised prompt/model as a new run.
