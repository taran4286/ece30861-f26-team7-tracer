# scripts/: database setup

`setup_db.py` creates the app role, the tables and the grants; group A owns it. The root "Database roles" section is the spec it implements, and "Local setup steps" says how to run it and read the result.

## How it's built

- `main()` reads both URLs, runs `setup()` in one owner transaction, then logs in through `DATABASE_URL` to confirm the app reaches the same server. Any `SetupError` or database error before the commit rolls everything back.
- `setup()` runs in this order: check `GRANTS` against `app.models`, upsert the app role, revoke its memberships, reset its attributes, check it owns neither the database nor `public`, revoke CREATE on `public`, `create_all`, apply the grants, then verify the effective privileges.
- `GRANTS` is what actually gets enforced. It must list exactly the tables in `app.models` (the script refuses to run otherwise) and must match the root grants list.
- It puts the repo root on `sys.path` so it can import `app`; nothing in `app/` imports it.

## When you change it

- New table or changed grant: update `GRANTS` and the root grants list together, then re-run the script locally and check the printed grid.
- New check: raise `SetupError` from inside `setup()`, so a failure rolls back and the message says nothing was changed.
- New script: give it a module docstring with its usage and exit codes, like `setup_db.py`, and add it to this file.
