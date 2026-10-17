# fixtures/: test fixtures

The static JSON items our tester sends to target systems. Root "Directory guides" says which file wins when this one disagrees with the root `AGENTS.md` or `system-openapi.yaml`.

| File | Contents |
| --- | --- |
| `incidents.json` | 10 `CreateIncidentRequest` bodies, one per discovery date from 2026-09-25 to 2026-10-04 |
| `ticketing.json` | 20 `TicketingItem`s with distinct `ticket_id`s; half carry the optional `description` |
| `siem.json` | 20 `SiemItem`s with distinct `alert_id`s, five of each severity; half carry `description` |

## Rules

- Each file is a JSON array of items that match the `system-openapi.yaml` schema exactly, so the tester sends them as they are.
- Every date is on or before 2026-10-05, the date of the spec's default `test_now`, and every timestamp is ISO-8601 UTC ending in `Z`.
