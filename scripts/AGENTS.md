# scripts/: database setup

`setup_db.py` creates the app and tester roles, all six tables and the grants, running as the owner role. Root "Database roles" says which role is which, and "Local setup steps" says how to run it and read the result.

## What it applies

Each run resets both login roles, so leftover privileges don't survive, then grants exactly:

| Role | Schema `public` | Tables |
| --- | --- | --- |
| App (`GRANTS`) | USAGE | `test_sessions`: SELECT, INSERT; `incidents`: SELECT, INSERT, UPDATE; `evidence`: SELECT, INSERT; `determinations`: SELECT, INSERT; `audit_entries`: SELECT, INSERT |
| Tester (`TESTER_GRANTS`) | USAGE | `tester_sessions`: SELECT, INSERT, UPDATE |

Nothing else: no DELETE or TRUNCATE anywhere, no UPDATE on `test_sessions`, `evidence`, `determinations` or `audit_entries`, and neither role gets anything on the other's tables. `audit_entries.seq` is an identity column, so the app role needs no sequence grant.

## How it's built

- Each role the deployed code connects as is a `Login`: the variable holding its URL (`DATABASE_URL` or `TESTER_DATABASE_URL`), the URL, and its grants. `SetupSettings` reads all three URLs from the environment or `.env`; `app/config.py` never reads `ADMIN_DATABASE_URL`.
- `main()` checks that each login URL has a user and password and names the same database as `ADMIN_DATABASE_URL`, and that the two logins use different roles. It runs `setup()` in one owner transaction, then logs in through each login URL and compares a server fingerprint (role OID, postmaster start time, database name) to confirm it reaches the same server.
- `setup()` runs in this order:
  1. Check `GRANTS` against `app.models` and `TESTER_GRANTS` against `tester.models`: each must list exactly the tables in its models module.
  2. For each login: refuse a user that is the owner; create its role, or set its password if it exists; revoke every role membership it holds; check `rolsuper`, `rolcreatedb`, `rolcreaterole`, `rolreplication` and `rolbypassrls`, clearing them with `ALTER ROLE` only when the owner is a superuser and failing otherwise; and fail if it owns the database or schema `public`, since a schema's owner can drop any table in it.
  3. Revoke CREATE on `public` from `PUBLIC`.
  4. `create_all` for both models, which creates only missing tables and types.
  5. Apply the grants: the owner takes ownership of all six tables and every enum type they use; on each table, all privileges are revoked from `PUBLIC` and both roles; then each role gets USAGE on `public` and its own grants.
  6. Verify each role with `has_table_privilege` on every table in both grant maps (SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, plus MAINTAIN on Postgres 17 and later) and `has_schema_privilege` on `public` (USAGE yes, CREATE no). It prints each role's grid and fails on any mismatch; the grids are the AC-23 permission demo.
- It puts the repo root on `sys.path` so it can import `app` and `tester`; nothing in either imports it.

## Exit codes and messages

- 0: the last line is `setup_db: done; <roles> have exactly the expected privileges`.
- 1 with a `setup_db:` message:
  - A bad login URL (no user or password, another database, the same role twice) fails before connecting.
  - A `SetupError` or database error inside `setup()` rolls everything back and ends `; nothing was changed`.
  - A failed login check after the commit says setup finished on the owner's server, so the changes stay.
- 1 with a Python traceback instead, because `main()` doesn't catch these:
  - a missing variable (pydantic `ValidationError`);
  - a malformed URL (from `make_url`);
  - a bare `postgresql://` scheme. On `ADMIN_DATABASE_URL` that fails before anything is written. On a login URL it fails at the login check, after the commit, and doesn't say the setup was applied.

## When you change it

- New table or changed grant: update `GRANTS` or `TESTER_GRANTS` and the table above together, re-run the script locally and check the printed grids, then run it against Neon (root "Running `setup_db.py` against Neon").
- New check: raise `SetupError` from inside `setup()`, so a failure rolls back and the message says nothing was changed.
- New script: give it a module docstring with its usage and exit codes, like `setup_db.py`, and add it to this file.
