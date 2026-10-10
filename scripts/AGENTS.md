# scripts/: database setup

`setup_db.py` creates the app role, the tables and the grants; group A owns it. The root "Database roles" section is the spec it implements.

## Rules

- It runs only as the owner role, through `ADMIN_DATABASE_URL`. Nothing in `app/` imports it, and the running service never gets that URL.
- It runs in one transaction and is safe to re-run. Each run resets the app role: password, memberships, attributes, table ownership and privileges.
- `GRANTS` is the source of truth for the app role's privileges. It must list exactly the tables in `app.models` (the script refuses to run otherwise) and must match the root grants list.
- Never grant DELETE or TRUNCATE on any table, or UPDATE on `evidence`, `determinations` or `audit_entries` (Phase 4 adds UPDATE on `evidence` for tagging).
- It ends by checking the app role's effective privileges and fails on any difference. The printed grid is the AC-23 demo.
- Judge a run by its exit code: 0 is success, and the last line starts with `setup_db: done;`. 1 is failure, and the message says what went wrong and whether anything changed.
- It only creates missing tables and types; it never alters existing ones.

## When you change it

- New table or changed grant: update `GRANTS` and the root grants list together, then re-run the script locally.
- New script: give it a module docstring with its usage and exit codes, like `setup_db.py`, and add it to this file.
