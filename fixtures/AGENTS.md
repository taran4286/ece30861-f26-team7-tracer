# fixtures/: test fixtures

The static JSON items our tester sends to target systems.

| File | Contents |
| --- | --- |
| `incidents.json` | 10 `CreateIncidentRequest` bodies, one per discovery date from 2026-09-25 to 2026-10-04 |
| `ticketing.json` | 20 `TicketingItem`s with distinct `ticket_id`s; half carry the optional `description` |
| `siem.json` | 20 `SiemItem`s with distinct `alert_id`s, five of each severity; half carry `description` |

## Rules

- Each file is a JSON array of items that match the `system-openapi.yaml` schema exactly, so the tester sends them as they are.
- Every date is on or before 2026-10-05, the `TODAY` the tester's cases assume, and every timestamp is ISO-8601 UTC ending in `Z`.
- `tester/cases.py` picks items by index, wrapping around, so changing a file's order or length changes what each case sends.
- The tester reads these files when `planned()` builds the case list, before a run starts, and caches them; they deploy with the service.
