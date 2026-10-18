# scripts/: database setup

`setup_db.py` creates the app and tester roles, the tables and the grants. The root "Database roles" section is the spec it implements, and "Local setup steps" says how to run it and read the result.

## How it's built

- Each role the deployed code connects as is a `Login`: the variable holding its URL (`DATABASE_URL` or `TESTER_DATABASE_URL`), the URL, and its grants.
- `main()` checks that each login URL has a user and password and names the same database as `ADMIN_DATABASE_URL`, and that the two logins use different roles. It runs `setup()` in one owner transaction, then logs in through each login URL to confirm it reaches the same server. Any `SetupError` or database error before the commit rolls everything back; the login checks run after the commit, so their failure leaves the setup applied.
- `setup()` runs in this order: check `GRANTS` against `app.models` and `TESTER_GRANTS` against `tester.models`; for each login, refuse a user that is the owner, upsert its role, revoke its memberships, check its attributes (cleared only when the owner is a superuser) and check it owns neither the database nor `public`; revoke CREATE on `public`; `create_all` for both models; apply the grants (the owner takes all tables and enum types, all privileges are revoked from both roles and `PUBLIC`, then each role gets its own); then verify each role's effective privileges.
- `GRANTS` (app role) and `TESTER_GRANTS` (tester role) are what actually get enforced. Each must list exactly the tables in its models module (the script refuses to run otherwise) and match the root grants lists. Each role is verified on every table in both, so a privilege on the other role's tables fails the run.
- It puts the repo root on `sys.path` so it can import `app` and `tester`; nothing in either imports it.

## When you change it

- New table or changed grant: update `GRANTS` or `TESTER_GRANTS` and the root grants list together, re-run the script locally and check the printed grids, then run it against Neon (root "Running `setup_db.py` against Neon").
- New check: raise `SetupError` from inside `setup()`, so a failure rolls back and the message says nothing was changed.
- New script: give it a module docstring with its usage and exit codes, like `setup_db.py`, and add it to this file.
