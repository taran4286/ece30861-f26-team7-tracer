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

The supplied Phase 5 paragraphs request 20 labeled texts for AC-18 but do not give its complete scoring procedure. This dataset interprets them as sensitive-detail detection examples. Confirm that interpretation and any detection threshold with the pod before using it as acceptance evidence. The 10 drafts and 40 statements are a separate reliability evaluation from the same paragraphs.

## Run a draft evaluation

1. Copy the CSV templates into a new results directory; keep the templates blank. Record the dataset version, code commit, provider/model identifier, prompt commit and run date. Freeze prompts and labels before collecting the final run. Keep raw outputs from failed attempts as well as successes; do not silently choose the best draft from repeated runs.
2. For each scenario, create an isolated session with its `registrant_type` and `test_now` from the fixture. Use `POST /test/setup`, `POST /incidents`, and `POST /incidents/{id}/evidence/import`. Send `incident` as the create body and `evidence_import` as the import body. Use Responder identity headers and that session's `X-Test-Run` on project calls. Record each returned evidence ID against its ticket ID in `evidence_map.csv`.
3. As DisclosureApprover, submit the scenario's `determination`, adding `version` from the incident and `evidence_refs` containing the imported IDs. Then call `POST /incidents/{id}/drafts` and save the entire returned draft unchanged. Record the actual session, incident, draft and generation metadata in `draft_review.csv`. The server builds its own prompt; never send `reviewer_key`, proposed sensitive labels, or scoring sheets to the LLM. Do not use source ticket IDs as evidence citations.
4. A reviewer compares every draft with the imported evidence. Independently review before looking at another reviewer's judgments; resolve disagreements with written notes rather than averaging contradictory labels. Keep raw model output separate from corrected/rejected text. The examples' human-supplied Material determinations enable drafting but must not be restated or decided by the LLM.
5. Audit **every citation occurrence in all four elements**, not only the 40 sampled statements. A valid citation is an evidence ID actually imported for that incident. A valid ID does not establish that the evidence supports the claim. Record total/valid counts in `draft_review.csv`; a draft with factual text but no citations fails. Retain the raw draft and mapping so the counts can be checked.
6. Select statements using the fixed rule below, then fill the 40 slots. Review the entire draft for contradictions and materiality conclusions, not just the selected statements. Complete each element's coverage, unknown-information handling, and sensitive-detail check. Use `yes`/`no` for these fields, leaving unknown results blank until reviewed. A correct `Not yet determined` satisfies coverage for an element whose evidence is missing.
7. Complete the summary using the formulas below. No pass/fail decision is valid until all 10 drafts, 40 statement slots and all citation audits are complete, with reviewer names, dates and retained outputs. A drafting failure is a failed generation to report, not permission to delete the case or claim a smaller denominator.

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
| Sensitive-detail precision | TP / (TP + FP) across all 20 labeled texts | Record; exact AC-18 threshold needs confirmation |
| Sensitive-detail recall | TP / (TP + FN) across all 20 labeled texts | Record; exact AC-18 threshold needs confirmation |

Use `INCOMPLETE` for missing observations, `PASS` only for a fully measured criterion meeting its threshold, and `FAIL` for a completed measurement missing it. Undefined precision/recall is `N/A`, not 100%. Other coverage/placeholder/flag observations remain visible alongside these totals; don't hide them in the numerical averages.

If the reliability thresholds are missed, record the failures and the mitigation described in the supplied plan: replace affected elements with `Not yet determined`, and have the pod decide whether thresholds need revision. These materials do not implement automatic detection of unsupported prose or change the service's behavior. Retain original and mitigated outputs separately and evaluate any revised prompt/model as a new run.
