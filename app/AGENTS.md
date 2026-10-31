# app/: foundation modules and the data model

The building blocks every service uses, and the system's tables. Business logic lives in `services/` (see `services/AGENTS.md`) and the FastAPI app in `api/` (see `api/AGENTS.md`).

## Modules

| Module | Provides |
| --- | --- |
| `config.py` | `Settings` (`database_url`, from `DATABASE_URL`) through the cached `get_settings()` |
| `db.py` | `Base`, the cached `get_engine()`, and `transaction()`: one session per operation, committed on success and rolled back on any error. `get_engine()` reads `DATABASE_URL` through `parse_url(value, setting)`, which raises `ArgumentError`, a `SQLAlchemyError`, for a malformed URL, where SQLAlchemy alone would raise a plain `ValueError` that storage handlers miss. Its message names the setting, never the URL, which holds a password |
| `models.py` | The six tables (`TestSession` first), their enums, `SiemSeverity` (checked on import, not stored) and `new_id(prefix)` |
| `errors.py` | `ErrorCode`, `HTTP_STATUS` (each code's status) and `TracerError(code, message, field=None)`, whose `status` comes from that map; `to_dict()` returns `error`, `message` and `field`, with `field` as `None` when unset, which the API layer leaves out (see `api/AGENTS.md`). Two codes map to 409: `INVALID_TRANSITION` for a project state error and `CONFLICT`, raised only for a test session ID already in use |
| `roles.py` | `Role`, `Operation`, the `PERMISSIONS` map and `require_role(role, operation)`, which raises `FORBIDDEN_ROLE`; a role string that isn't a `Role` gets no permissions, so it also gets `FORBIDDEN_ROLE`. `Operation.REPLACE_OR_DELETE` covers the 405 routes: only DisclosureApprover passes it, and then gets 405 |
| `clock.py` | `today(session)`, the UTC date of the session's `test_now`, for every date rule, and `utcnow()` for stored timestamps |
| `sessions.py` | `load_session(tx, session_id)`: the `TestSession` a request names in `X-Test-Run`, or `NOT_FOUND` |
| `audit.py` | `audit(tx, action, target_id, before, after, *, user_id, session_id)`, which adds one entry inside the caller's transaction, with a new `entry_id` and `timestamp` from `utcnow()` |

## Data model

Six tables hold the system's data; the tester keeps its own in `tester_sessions` (see `tester/AGENTS.md`). IDs are opaque strings from `new_id()` except session IDs, which the caller supplies. Every timestamp column is `timestamp with time zone`, stored in UTC. JSON columns are `JSONB`.

**test_sessions** (`TestSession`)

| Column | Type | Notes |
| --- | --- | --- |
| session_id | text, primary key | supplied by `POST /test/setup` |
| test_now | timestamp, not null | its UTC date is today in the session |
| registrant_type | `registrant_type` enum, not null | |
| at_risk_threshold_days | integer, not null | |
| llm_base_url | text, not null | |
| created_at | timestamp, not null | real server time |

**incidents** (`Incident`)

| Column | Type | Notes |
| --- | --- | --- |
| incident_id | text, primary key | `inc_…` |
| session_id | text, not null, references test_sessions, indexed | the session that created it |
| title | text, not null | |
| discovery_date | date, not null | |
| state | `state` enum, not null | |
| required_form | `form_type` enum, nullable | |
| due_date | date, nullable | four business days after a Domestic Material determination; null for FPI or NotMaterial |
| group_id | text, nullable | always null |
| version | integer, not null | |

**determinations** (`Determination`)

| Column | Type | Notes |
| --- | --- | --- |
| incident_id | text, primary key, references incidents | so at most one per incident |
| decision | `decision` enum, not null | |
| determination_date | date, not null | |
| rationale | text, not null | |
| evidence_refs | JSONB array of evidence IDs, not null | |
| voluntary_disclosure | boolean, not null | always false |
| recorded_by | text, not null | the acting user |
| recorded_at | timestamp, not null | real server time |

**filings** (`Filing`)

No service reads or writes this table; `scripts/setup_db.py` creates it and its grants.

| Column | Type | Notes |
| --- | --- | --- |
| seq | bigint `Identity()`, primary key | sets "oldest first" order, so the latest filing has the highest `seq`; never returned by the API, whose `Filing` has no ID |
| incident_id | text, not null, references incidents, indexed | |
| form_type | `form_type` enum, not null | the same type as `incidents.required_form` |
| filing_date | date, not null | |
| accession_number | text, not null | |
| unavailable_elements | JSONB array of element names, not null | |
| home_disclosure_date | date, nullable | |
| recorded_by | text, not null | the acting user |
| recorded_at | timestamp, not null | real server time |

**evidence** (`Evidence`)

| Column | Type | Notes |
| --- | --- | --- |
| evidence_id | text, primary key | `ev_…` |
| incident_id | text, not null, references incidents, indexed | |
| source | `evidence_source` enum, not null | |
| source_ref | text, not null | from `ticket_id` or `alert_id` |
| summary | text, not null | from `summary` or `rule_name` |
| observed_at | timestamp, not null | from `created_at` or `detected_at` |
| imported_by | text, not null | the importing user |
| imported_at | timestamp, not null | real server time |
| impact_categories | JSONB array, not null | always empty |
| raw_item | JSONB, not null | the item as sent; never returned by the API |

**audit_entries** (`AuditEntry`)

| Column | Type | Notes |
| --- | --- | --- |
| seq | bigint `Identity()`, primary key | sets "oldest first" order; an identity column, so the app role needs no sequence grant |
| entry_id | text, unique, not null | `aud_…` |
| session_id | text, not null, references test_sessions, indexed | the request's session |
| user_id | text, not null | the acting user |
| timestamp | timestamp, not null | real server time |
| action | `audit_action` enum, not null | |
| target_id | text, not null, indexed | the incident ID |
| before | JSONB, nullable | SQL `NULL` on create and import |
| after | JSONB, not null | |

**Enums** (each stored as the Postgres enum type named in the tables above, by value)

- `State`: UnderAssessment, DeterminedMaterial, DeterminedNotMaterial, DisclosureDelayed, DisclosureRecorded, AmendmentPending, Closed.
- `FormType`: `8-K Item 1.05`, `8-K Item 8.01`, `6-K`, `8-K/A`.
- `Decision`: Material, NotMaterial.
- `EvidenceSource`: ticketing, siem.
- `AuditAction`: create_incident, import_evidence, record_determination, transition, record_filing. No service writes `record_filing`.
- `RegistrantType`: Domestic, FPI.
- Not stored as a type: `ImpactCategory` (operational, financial, legal_regulatory, reputational, data_compromised) and `SiemSeverity` (low, medium, high, critical, checked on import).

## Rules

- Keep `app/` free of web framework imports, apart from `app/api/`; services must stay callable without one.
- Never call `datetime.now()` or `date.today()` outside `clock.py`; use `clock`. Date rules use `today(session)`, never the real date.
- Raise only `TracerError`, with a path-style `field` on validation errors. A new `ErrorCode` needs its status in `HTTP_STATUS`.
- Loaders (`load_session`, `load_incident`, `load_evidence`) treat an ID containing NUL as unknown without querying: PostgreSQL text can't hold NUL, so the query would fail instead of returning nothing.
- Make IDs with `new_id()`. Prefixes in use: `inc`, `ev`, `aud`. Session IDs are the exception: the caller supplies them.
- `TestSession` sets `__test__ = False`, so pytest doesn't collect it as a test class when a test file imports it.
- `get_settings()`, `get_engine()` and the session factory behind `transaction()` (`_session_factory()`) are cached. A test that changes a setting, `DATABASE_URL` included, clears all three before and after; otherwise later code keeps the old settings or database.
- `get_engine()` sets `pool_pre_ping`, so a pooled connection the server dropped while idle is replaced before use instead of failing the operation. Neon drops them whenever it pauses (see the root "Deployment" section); keep it on.
- `get_engine()` also passes `connect_timeout` (`CONNECT_TIMEOUT_SECONDS`, 5 s). psycopg applies it to each address the host resolves to, so a Neon database that stops answering (three addresses) fails in about 15 s. Without it, psycopg waits 130 s per address, longer than Vercel's 300 s function limit. `pool_pre_ping` doesn't bound that wait.
- JSON columns accept `date` and `datetime` values directly; `db.py` stores them as ISO-8601 strings.
- Enums are stored by value (`"8-K Item 1.05"`), not by member name, through `_db_enum()`.
- `AuditEntry.before` stores Python `None` as SQL `NULL`, not JSON `null`.

## When you change `models.py`

- Update the Data model section above to match.
- Adding a table: add it to `GRANTS` in `scripts/setup_db.py` (the script refuses to run otherwise), update `scripts/AGENTS.md`, and re-run the script.
- Adding an enum value: re-run the setup script, which adds it. Changing a column, or removing or renaming an enum value: follow the notes under root "Local setup steps"; the setup script doesn't alter existing columns.
- Adding an `AuditAction`: list it above, and document its `before` and `after` shapes under its operation in `services/AGENTS.md`.
