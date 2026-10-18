"""Create the app and tester roles, the tables and the grants. Run as the owner role.

Usage: python scripts/setup_db.py

Reads ADMIN_DATABASE_URL (owner), DATABASE_URL (app role) and TESTER_DATABASE_URL
(tester role) from the environment or .env. Runs in one transaction and is safe to
re-run: each run resets both login roles so leftover privileges don't survive, then
checks each one's effective privileges on every table and fails if any differ from
GRANTS or TESTER_GRANTS. The printed grids are the AC-23 demo. Finally it logs in
through each role's URL to confirm it reaches the same server.

Exits 0 on success and 1 on failure. It only creates missing tables and types, so
changes to an existing table need a wiped local database or a migration.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Connection, Enum, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base  # noqa: E402
from app.models import AuditEntry, Determination, Evidence, Incident, TestSession  # noqa: E402
from tester.db import Base as TesterBase  # noqa: E402
from tester.models import StoredSession  # noqa: E402

SCHEMA = "public"

# Exactly what the app role gets (REQ-22). No DELETE or TRUNCATE anywhere.
GRANTS: dict[str, frozenset[str]] = {
    TestSession.__tablename__: frozenset({"SELECT", "INSERT"}),
    Incident.__tablename__: frozenset({"SELECT", "INSERT", "UPDATE"}),
    # Phase 4 adds UPDATE for tagging.
    Evidence.__tablename__: frozenset({"SELECT", "INSERT"}),
    Determination.__tablename__: frozenset({"SELECT", "INSERT"}),
    AuditEntry.__tablename__: frozenset({"SELECT", "INSERT"}),
}

# Exactly what the tester role gets: its own table, with no DELETE or TRUNCATE either.
TESTER_GRANTS: dict[str, frozenset[str]] = {
    StoredSession.__tablename__: frozenset({"SELECT", "INSERT", "UPDATE"}),
}

# Each role's privileges are checked on all of these, so neither reaches the other's tables.
TABLES = (*GRANTS, *TESTER_GRANTS)

METADATA = (Base.metadata, TesterBase.metadata)

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
    tester_database_url: str


@dataclass(frozen=True)
class Login:
    """A role the deployed code connects as: the variable holding its URL, and its exact grants."""

    env: str
    url: URL
    grants: dict[str, frozenset[str]]

    @property
    def role(self) -> str:
        return self.url.username


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


def _upsert_role(conn: Connection, role: str, password: str) -> None:
    exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    verb = "ALTER" if exists else "CREATE"
    _exec_fmt(conn, f"{verb} ROLE %I LOGIN PASSWORD %L", role, password)


def _memberships(conn: Connection, role: str) -> list[tuple[str, str]]:
    """(role, grantor) for every role this role is a member of."""
    rows = conn.execute(
        text(
            "SELECT pg_get_userbyid(m.roleid), pg_get_userbyid(m.grantor) "
            "FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname = :r"
        ),
        {"r": role},
    )
    return [(member_of, grantor) for member_of, grantor in rows]


def _revoke_memberships(conn: Connection, role: str) -> None:
    for member_of, grantor in _memberships(conn, role):
        _exec_fmt(conn, "REVOKE %I FROM %I GRANTED BY %I", member_of, role, grantor)
    if left := _memberships(conn, role):
        raise SetupError(f"{role} is still a member of {sorted({r for r, _ in left})}")


def _reset_attributes(conn: Connection, role: str, owner_is_superuser: bool) -> None:
    row = conn.execute(
        text(f"SELECT {', '.join(FORBIDDEN_ATTRIBUTES)} FROM pg_roles WHERE rolname = :r"),
        {"r": role},
    ).one()
    set_attributes = [name for name, value in zip(FORBIDDEN_ATTRIBUTES, row) if value]
    if not set_attributes:
        return
    if not owner_is_superuser:
        raise SetupError(
            f"{role} has {', '.join(set_attributes)}; the owner isn't a superuser, "
            "so clear them as the host's admin and re-run"
        )
    _exec_fmt(
        conn,
        "ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS",
        role,
    )


def _check_ownership(conn: Connection, role: str) -> None:
    # A database or schema owner can drop every table in it; no revoke catches that.
    db_owner = conn.execute(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
    ).scalar_one()
    schema_owner = conn.execute(
        text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = :s"),
        {"s": SCHEMA},
    ).scalar_one()
    if role == db_owner:
        raise SetupError(f"{role} owns the database; make the owner role own it instead")
    if role == schema_owner:
        raise SetupError(f"{role} owns schema {SCHEMA}; make the owner role own it instead")


def _enum_types() -> list[str]:
    return sorted(
        {
            column.type.name
            for metadata in METADATA
            for table in metadata.tables.values()
            for column in table.columns
            if isinstance(column.type, Enum)
        }
    )


def _apply_grants(conn: Connection, owner: str, logins: list[Login]) -> None:
    for table in TABLES:
        _exec_fmt(conn, "ALTER TABLE %I OWNER TO %I", table, owner)
        _exec_fmt(conn, "REVOKE ALL ON TABLE %I FROM PUBLIC", table)
        for login in logins:
            _exec_fmt(conn, "REVOKE ALL ON TABLE %I FROM %I", table, login.role)
    for type_name in _enum_types():
        _exec_fmt(conn, "ALTER TYPE %I OWNER TO %I", type_name, owner)

    for login in logins:
        _exec_fmt(conn, "GRANT USAGE ON SCHEMA %I TO %I", SCHEMA, login.role)
        for table, privileges in login.grants.items():
            _exec_fmt(
                conn, f"GRANT {', '.join(sorted(privileges))} ON TABLE %I TO %I", table, login.role
            )


def _verify(conn: Connection, login: Login) -> None:
    """Check a role's effective privileges on every table, print them, and fail on any mismatch."""
    version = int(conn.execute(text("SHOW server_version_num")).scalar_one())
    privileges = TABLE_PRIVILEGES + (("MAINTAIN",) if version >= 170000 else ())

    rows: list[tuple[str, str, bool, bool]] = []
    for table in TABLES:
        granted = login.grants.get(table, frozenset())
        for privilege in privileges:
            actual = conn.execute(
                text(
                    "SELECT has_table_privilege("
                    "CAST(:r AS name), CAST(:t AS text), CAST(:p AS text))"
                ),
                {"r": login.role, "t": f"{SCHEMA}.{table}", "p": privilege},
            ).scalar_one()
            rows.append((table, privilege, privilege in granted, actual))
    for privilege, expected in (("USAGE", True), ("CREATE", False)):
        actual = conn.execute(
            text(
                "SELECT has_schema_privilege("
                "CAST(:r AS name), CAST(:s AS text), CAST(:p AS text))"
            ),
            {"r": login.role, "s": SCHEMA, "p": privilege},
        ).scalar_one()
        rows.append((f"schema {SCHEMA}", privilege, expected, actual))

    width = max(len(target) for target, *_ in rows)
    print(f"Effective privileges of {login.role}:")
    print(f"  {'target':<{width}}  {'privilege':<10}  expected  actual")
    mismatches = []
    for target, privilege, expected, actual in rows:
        mark = "" if expected == actual else "  <-- MISMATCH"
        print(f"  {target:<{width}}  {privilege:<10}  {_yn(expected):<8}  {_yn(actual)}{mark}")
        if expected != actual:
            mismatches.append(f"{target} {privilege}")
    if mismatches:
        raise SetupError(f"{login.role}'s privileges differ from its grants: {', '.join(mismatches)}")


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _server_fingerprint(conn: Connection, role: str) -> tuple[Any, ...]:
    """Identifies the server and database a connection reached.

    Compared across the owner's and each role's connections instead of their URLs, which
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


def _check_connection(login: Login, expected: tuple[Any, ...]) -> str | None:
    """Log in through a role's URL and confirm it reaches the server setup just changed."""
    engine = create_engine(login.url)
    try:
        with engine.connect() as conn:
            actual = _server_fingerprint(conn, login.role)
    except DBAPIError as error:
        return f"{login.role} can't connect through {login.env}: {error.orig}"
    finally:
        engine.dispose()
    if actual != expected:
        return f"{login.env} reaches a different server or database than ADMIN_DATABASE_URL"
    return None


def setup(conn: Connection, logins: list[Login]) -> dict[str, tuple[Any, ...]]:
    """Apply everything in one transaction; returns each role's server fingerprint for its login check."""
    if set(Base.metadata.tables) != set(GRANTS):
        raise SetupError(f"GRANTS must list exactly the tables in app.models: {sorted(Base.metadata.tables)}")
    if set(TesterBase.metadata.tables) != set(TESTER_GRANTS):
        raise SetupError(
            f"TESTER_GRANTS must list exactly the tables in tester.models: {sorted(TesterBase.metadata.tables)}"
        )

    conn.execute(text(f"SET LOCAL search_path TO {SCHEMA}"))
    owner, owner_is_superuser = conn.execute(
        text("SELECT rolname, rolsuper FROM pg_roles WHERE rolname = current_user")
    ).one()

    for login in logins:
        if login.role == owner:
            raise SetupError(f"{login.env} must use its own role, not the owner role")
        _upsert_role(conn, login.role, login.url.password)
        _revoke_memberships(conn, login.role)
        _reset_attributes(conn, login.role, owner_is_superuser)
        _check_ownership(conn, login.role)
    _exec_fmt(conn, "REVOKE CREATE ON SCHEMA %I FROM PUBLIC", SCHEMA)

    for metadata in METADATA:
        metadata.create_all(conn)
    _apply_grants(conn, owner, logins)
    for login in logins:
        _verify(conn, login)
    return {login.role: _server_fingerprint(conn, login.role) for login in logins}


def main() -> int:
    settings = SetupSettings()
    admin_url = make_url(settings.admin_database_url)
    logins = [
        Login("DATABASE_URL", make_url(settings.database_url), GRANTS),
        Login("TESTER_DATABASE_URL", make_url(settings.tester_database_url), TESTER_GRANTS),
    ]
    for login in logins:
        if not login.url.username or not login.url.password:
            print(f"setup_db: {login.env} needs its role's user and password", file=sys.stderr)
            return 1
        if admin_url.database != login.url.database:
            print(f"setup_db: ADMIN_DATABASE_URL and {login.env} name different databases", file=sys.stderr)
            return 1
    if len({login.role for login in logins}) != len(logins):
        print("setup_db: DATABASE_URL and TESTER_DATABASE_URL must use different roles", file=sys.stderr)
        return 1

    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            fingerprints = setup(conn, logins)
    except SetupError as error:
        print(f"setup_db: {error}; nothing was changed", file=sys.stderr)
        return 1
    except DBAPIError as error:
        print(f"setup_db: {error.orig}; nothing was changed", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    # Setup is committed by now, so a failure here can't say "nothing was changed".
    for login in logins:
        if problem := _check_connection(login, fingerprints[login.role]):
            print(
                f"setup_db: setup finished on the ADMIN_DATABASE_URL server, but {problem}",
                file=sys.stderr,
            )
            return 1

    roles = " and ".join(login.role for login in logins)
    print(f"setup_db: done; {roles} have exactly the expected privileges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
