# fixtures/: shared test fixtures

The static JSON items that both pod teams' testers send to target systems. The root `AGENTS.md` wins wherever this file disagrees.

| File | Contents |
| --- | --- |
| `incidents.json` | 10 `CreateIncidentRequest` bodies, one per discovery date from 2026-09-25 to 2026-10-04 |
| `ticketing.json` | 20 `TicketingItem`s with distinct `ticket_id`s; half carry the optional `description` |
| `siem.json` | 20 `SiemItem`s with distinct `alert_id`s, five of each severity; half carry `description` |

## Rules

- Each file is a JSON array of items that match the `system-openapi.yaml` schema exactly, so the tester sends them as they are.
- Every date is on or before 2026-10-05, the date of the spec's default `test_now`, and every timestamp is ISO-8601 UTC ending in `Z`.
- Team 10 keeps its own copy. The teams agree on a change before either copy is edited.
- `tester/cases.py` picks items by index, wrapping around, so changing a file's order or length changes what each case sends.
- The tester reads these files when a case first needs them, so they deploy with the service.
