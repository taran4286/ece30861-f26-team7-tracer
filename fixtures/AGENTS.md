# fixtures/: test fixtures

The static JSON items our tester sends to target systems.

| File | Contents |
| --- | --- |
| `incidents.json` | 10 `CreateIncidentRequest` bodies, one per discovery date from 2026-09-25 to 2026-10-04 |
| `ticketing.json` | 20 `TicketingItem`s with distinct `ticket_id`s; half carry the optional `description` |
| `siem.json` | 20 `SiemItem`s with distinct `alert_id`s, five of each severity; half carry `description` |
| `evaluation/` | Offline synthetic draft-evaluation materials: 20 sensitive-detail texts and proposed labels, 10 Domestic/FPI scenarios with source ticketing evidence and reviewer keys, blank CSV sheets for sensitive labels, 10 drafts, 40 statements, evidence-ID mapping and a summary; `evaluation/README.md` defines the protocol and thresholds. No measured human/model results are included |

The evaluation set includes five missing-impact cases with a dedicated sheet, attempt/capture and third-reviewer adjudication logs. The protocol specifies provider URL/model/private server authentication, capture before validation (with an optional local `_parse` hook and its limitations), two independent reviewers, third-person dispute resolution, AC-17's 5/5 exact placeholders and AC-18's text-level thresholds (at least 9/10 sensitive texts flagged, at most 2/10 clean texts flagged). Span precision/recall are supplemental. All scoring templates remain unfilled.

## Rules

- Each top-level JSON file is an array of items that match the `system-openapi.yaml` schema exactly, so the tester sends them as they are. The offline `evaluation/` datasets are structured objects with metadata and reviewer-only expectations, not API request bodies; send only the named incident/import bodies, map generated evidence IDs, and never supply reviewer keys to a provider.
- Every date is on or before 2026-10-05, the `TODAY` the tester's cases assume, and every timestamp is ISO-8601 UTC ending in `Z`.
- `tester/cases.py` picks items by index, wrapping around, so changing a file's order or length changes what each case sends.
- The tester reads these files when `planned()` builds the case list, before a run starts, and caches them; they deploy with the service.
