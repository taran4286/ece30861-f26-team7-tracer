"""Create the app role, the tables and the grants. Run as the owner role.

Usage: python scripts/setup_db.py

Reads ADMIN_DATABASE_URL (owner) and DATABASE_URL (app role) from the environment
or .env. Runs in one transaction and is safe to re-run: each run resets the app
role so leftover privileges don't survive, then checks the app role's effective
privileges and fails if any differ from GRANTS. The printed grid is the AC-23 demo.
Finally it logs in through DATABASE_URL to confirm the app reaches the same server.

Exits 0 on success and 1 on failure. It only creates missing tables and types, so
changes to an existing table need a wiped local database or a migration.
"""

import sys
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, Enum, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base  # noqa: E402
from app.models import AuditEntry, Determination, Evidence, Incident  # noqa: E402

SCHEMA = "public"

# Exactly what the app role gets (REQ-22). No DELETE or TRUNCATE anywhere.
GRANTS: dict[str, frozenset[str]] = {
    Incident.__tablename__: frozenset({"SELECT", "INSERT", "UPDATE"}),
    # Phase 4 adds UPDATE for tagging.
    Evidence.__tablename__: frozenset({"SELECT", "INSERT"}),
    Determination.__tablename__: frozenset({"SELECT", "INSERT"}),
    AuditEntry.__tablename__: frozenset({"SELECT", "INSERT"}),
}

# Checked on every table; MAINTAIN is added on Postgres 17+.
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

FORBIDDEN_ATTRIBUTES = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
)


class SetupSettings(BaseSettings):
    # Local to this script so app/config.py never reads the owner's credentials.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    admin_database_url: str
    database_url: str


class SetupError(Exception):
    pass


def _exec_fmt(conn: Connection, template: str, *args: str) -> None:
    """Run a utility statement whose identifiers and literals can't be bind parameters.

    Postgres's format() quotes them server-side: %I for identifiers, %L for literals.
    """
    casts = "".join(f", CAST(:a{i} AS text)" for i in range(len(args)))
    params = {f"a{i}": arg for i, arg in enumerate(args)}
    stmt = conn.execute(
        text(f"SELECT format(CAST(:template AS text){casts})"), {"template": template, **params}
    ).scalar_one()
    # no_parameters: a '%' inside a quoted password must not read as a placeholder.
    conn.exec_driver_sql(stmt, execution_options={"no_parameters": True})


def _upsert_app_role(conn: Connection, app_role: str, password: str) -> None:
    exists = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": app_role}
    ).first()
    verb = "ALTER" if exists else "CREATE"
    _exec_fmt(conn, f"{verb} ROLE %I LOGIN PASSWORD %L", app_role, password)


def _memberships(conn: Connection, app_role: str) -> list[tuple[str, str]]:
    """(role, grantor) for every role the app role is a member of."""
    rows = conn.execute(
        text(
            "SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.grantor) "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = :r"
        ),
        {"r": app_role},
    )
    return [(role, grantor) for role, grantor in rows]


def _revoke_memberships(conn: Connection, app_role: str) -> None:
    for role, grantor in _memberships(conn, app_role):
        _exec_fmt(conn, "REVOKE %I FROM %I GRANTED BY %I", role, app_role, grantor)
    if left := _memberships(conn, app_role):
        raise SetupError(f"{app_role} is still a member of {sorted({r for r, _ in left})}")


def _reset_attributes(conn: Connection, app_role: str, owner_is_superuser: bool) -> None:
    row = conn.execute(
        text(f"SELECT {', '.join(FORBIDDEN_ATTRIBUTES)} FROM pg_roles WHERE rolname = :r"),
        {"r": app_role},
    ).one()
    set_attributes = [name for name, value in zip(FORBIDDEN_ATTRIBUTES, row) if value]
    if not set_attributes:
        return
    if not owner_is_superuser:
        raise SetupError(
            f"{app_role} has {', '.join(set_attributes)}; the owner isn't a superuser, "
            "so clear them as the host's admin and re-run"
        )
    _exec_fmt(
        conn,
        "ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        app_role,
    )


def _check_ownership(conn: Connection, app_role: str) -> None:
    # A database or schema owner can drop every table in it; no revoke catches that.
    db_owner = conn.execute(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
    ).scalar_one()
    schema_owner = conn.execute(
        text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :s"),
        {"s": SCHEMA},
    ).scalar_one()
    if app_role == db_owner:
        raise SetupError(f"{app_role} owns the database; make the owner role own it instead")
    if app_role == schema_owner:
        raise SetupError(f"{app_role} owns schema {SCHEMA}; make the owner role own it instead")


def _enum_types() -> list[str]:
    return sorted(
        {
            column.type.name
            for table in Base.metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, Enum)
        }
    )


def _apply_grants(conn: Connection, owner: str, app_role: str) -> None:
    for table in GRANTS:
        _exec_fmt(conn, "ALTER TABLE %I OWNER TO %I", table, owner)
        _exec_fmt(conn, "REVOKE ALL ON TABLE %I FROM %I, PUBLIC", table, app_role)
    for type_name in _enum_types():
        _exec_fmt(conn, "ALTER TYPE %I OWNER TO %I", type_name, owner)

    _exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", SCHEMA, app_role)
    for table, privileges in GRANTS.items():
        _exec_fmt(conn, f"GRANT {', '.join(sorted(privileges))} ON TABLE %I TO %I", table, app_role)


def _verify(conn: Connection, app_role: str) -> None:
    """Check the app role's effective privileges, print them, and fail on any mismatch."""
    version = int(conn.execute(text("SHOW server_version_num")).scalar_one())
    privileges = TABLE_PRIVILEGES + (("MAINTAIN",) if version >= 170000 else ())

    rows: list[tuple[str, str, bool, bool]] = []
    for table, granted in GRANTS.items():
        for privilege in privileges:
            actual = conn.execute(
                text(
                    "SELECT has_table_privilege("
                    "CAST(:r AS name), CAST(:t AS text), CAST(:p AS text))"
                ),
                {"r": app_role, "t": f"{SCHEMA}.{table}", "p": privilege},
            ).scalar_one()
            rows.append((table, privilege, privilege in granted, actual))
    for privilege, expected in (("USAGE", True), ("CREATE", False)):
        actual = conn.execute(
            text(
                "SELECT has_schema_privilege("
                "CAST(:r AS name), CAST(:s AS text), CAST(:p AS text))"
            ),
            {"r": app_role, "s": SCHEMA, "p": privilege},
        ).scalar_one()
        rows.append((f"schema {SCHEMA}", privilege, expected, actual))

    width = max(len(target) for target, *_ in rows)
    print(f"Effective privileges of {app_role}:")
    print(f"  {'target':<{width}}  {'privilege':<10}  expected  actual")
    mismatches = []
    for target, privilege, expected, actual in rows:
        mark = "" if expected == actual else "  <-- MISMATCH"
        print(f"  {target:<{width}}  {privilege:<10}  {_yn(expected):<8}  {_yn(actual)}{mark}")
        if expected != actual:
            mismatches.append(f"{target} {privilege}")
    if mismatches:
        raise SetupError(f"privileges differ from GRANTS: {', '.join(mismatches)}")


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _server_fingerprint(conn: Connection, role: str) -> tuple[Any, ...]:
    """Identifies the server and database a connection reached.

    Compared across the owner's and the app's connections instead of their URLs, which
    can differ for the same server (localhost vs 127.0.0.1, a host's pooled endpoint).
    """
    return tuple(
        conn.execute(
            text(
                "SELECT r.oid, pg_postmaster_start_time(), current_database() "
                "FROM pg_roles r WHERE r.rolname = :r"
            ),
            {"r": role},
        ).one()
    )


def _check_app_connection(app_url: URL, expected: tuple[Any, ...]) -> str | None:
    """Log in through DATABASE_URL and confirm it reaches the server setup just changed."""
    engine = create_engine(app_url)
    try:
        with engine.connect() as conn:
            actual = _server_fingerprint(conn, app_url.username)
    except DBAPIError as error:
        return f"{app_url.username} can't connect through DATABASE_URL: {error.orig}"
    finally:
        engine.dispose()
    if actual != expected:
        return "DATABASE_URL reaches a different server or database than ADMIN_DATABASE_URL"
    return None


def setup(conn: Connection, app_role: str, app_password: str) -> tuple[Any, ...]:
    """Apply everything in one transaction; returns the server fingerprint for the app check."""
    if set(Base.metadata.tables) != set(GRANTS):
        raise SetupError(f"GRANTS must list exactly the tables in app.models: {sorted(Base.metadata.tables)}")

    conn.execute(text(f"SET LOCAL search_path TO {SCHEMA}"))
    owner, owner_is_superuser = conn.execute(
        text("SELECT rolname, rolsuper FROM pg_roles WHERE rolname = current_user")
    ).one()
    if app_role == owner:
        raise SetupError("DATABASE_URL must use the app role, not the owner role")

    _upsert_app_role(conn, app_role, app_password)
    _revoke_memberships(conn, app_role)
    _reset_attributes(conn, app_role, owner_is_superuser)
    _check_ownership(conn, app_role)
    _exec_fmt(conn, "REVOKE CREATE ON SCHEMA %I FROM PUBLIC", SCHEMA)

    Base.metadata.create_all(conn)
    _apply_grants(conn, owner, app_role)
    _verify(conn, app_role)
    return _server_fingerprint(conn, app_role)


def main() -> int:
    settings = SetupSettings()
    admin_url = make_url(settings.admin_database_url)
    app_url = make_url(settings.database_url)
    if not app_url.username or not app_url.password:
        print("setup_db: DATABASE_URL needs the app role's user and password", file=sys.stderr)
        return 1
    if admin_url.database != app_url.database:
        print("setup_db: ADMIN_DATABASE_URL and DATABASE_URL name different databases", file=sys.stderr)
        return 1

    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            fingerprint = setup(conn, app_url.username, app_url.password)
    except SetupError as error:
        print(f"setup_db: {error}; nothing was changed", file=sys.stderr)
        return 1
    except DBAPIError as error:
        print(f"setup_db: {error.orig}; nothing was changed", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    # Setup is committed by now, so a failure here can't say "nothing was changed".
    if problem := _check_app_connection(app_url, fingerprint):
        print(
            f"setup_db: setup finished on the ADMIN_DATABASE_URL server, but {problem}",
            file=sys.stderr,
        )
        return 1

    print(f"setup_db: done; {app_url.username} has exactly the expected privileges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
