# app/: foundation modules

The building blocks every service uses. Business logic lives in `services/` (see `services/AGENTS.md`) and the FastAPI app in `api/` (see `api/AGENTS.md`). The root `AGENTS.md` holds the spec these modules implement; its "Directory guides" section says which file wins when they disagree.

## Modules

| Module | Provides |
| --- | --- |
| `config.py` | `Settings` (`database_url`) through the cached `get_settings()` |
| `db.py` | `Base`, the cached `get_engine()`, and `transaction()`: one session per operation, committed on success and rolled back on any error. `get_engine()` reads `DATABASE_URL` through `parse_url()`, which raises `ArgumentError`, a `SQLAlchemyError`, for a malformed URL, where SQLAlchemy alone would raise a plain `ValueError` that storage handlers miss |
| `models.py` | The five tables (`TestSession` first), their enums, `SiemSeverity` (checked on import, not stored) and `new_id(prefix)` |
| `errors.py` | `ErrorCode`, `HTTP_STATUS` (each code's status) and `TracerError(code, message, field=None)`, whose `status` comes from that map; `to_dict()` returns `error`, `message` and `field`, with `field` as `None` when unset, while the Error schema wants it left out (see `api/AGENTS.md`) |
| `roles.py` | `Role`, `Operation`, the `PERMISSIONS` map and `require_role(role, operation)`. `Operation.REPLACE_OR_DELETE` covers the 405 routes: only DisclosureApprover passes it, and then gets 405 |
| `clock.py` | `today(session)`, the UTC date of the session's `test_now`, for every date rule, and `utcnow()` for stored timestamps |
| `sessions.py` | `load_session(tx, session_id)`: the `TestSession` a request names in `X-Test-Run`, or `NOT_FOUND` |
| `audit.py` | `audit(tx, action, target_id, before, after, *, user_id, session_id)`, which adds one entry inside the caller's transaction |

## Rules

- Keep `app/` free of web framework imports, apart from `app/api/`; services must stay callable without one.
- Never call `datetime.now()` or `date.today()` outside `clock.py`; use `clock` (root "Dates").
- Raise only `TracerError`, with a path-style `field` on validation errors. A new `ErrorCode` needs its status in `HTTP_STATUS`.
- Loaders (`load_session`, `load_incident`, `load_evidence`) treat an ID containing NUL as unknown without querying: PostgreSQL text can't hold NUL, so the query would fail instead of returning nothing.
- Make IDs with `new_id()`. Prefixes in use: `inc`, `ev`, `aud`. Session IDs are the exception: the tester supplies them.
- `TestSession` sets `__test__ = False`, so pytest doesn't collect it as a test class when a test file imports it.
- `config.py` never reads `ADMIN_DATABASE_URL`; only `scripts/setup_db.py` does.
- `get_settings()`, `get_engine()` and the session factory behind `transaction()` (`_session_factory()`) are cached. A test that changes a setting, `DATABASE_URL` included, clears all three before and after; otherwise later code keeps the old settings or database.
- `get_engine()` sets `pool_pre_ping`, so a pooled connection the server dropped while idle is replaced before use instead of failing the operation. Neon drops them whenever it pauses (see the root "Deployment" section); keep it on.
- `get_engine()` also passes `connect_timeout` (`CONNECT_TIMEOUT_SECONDS`, 5 s). psycopg applies it to each address the host resolves to, so a Neon database that stops answering (three addresses) fails in about 15 s. Without it, psycopg waits 130 s per address, longer than Vercel's 300 s function limit. `pool_pre_ping` doesn't bound that wait.
- JSON columns accept `date` and `datetime` values directly; `db.py` stores them as ISO-8601 strings.
- Enums are stored by value (`"8-K Item 1.05"`), not by member name, through `_db_enum()`.
- `AuditEntry.before` stores Python `None` as SQL `NULL`, not JSON `null`.
- Nothing here creates tables; the app role can't. `scripts/setup_db.py` does.

## When you change `models.py`

- Update the root "Data model and storage" section to match.
- Adding a table: add it to `GRANTS` in `scripts/setup_db.py` (the script refuses to run otherwise) and re-run the script.
- Changing a column or an enum's values: follow the notes under root "Local setup steps"; the setup script doesn't alter existing tables.
- Adding an `AuditAction`: list it in the root "Enums", and document its `before` and `after` shapes in the root section for its operation and in "Audit trail".
