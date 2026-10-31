"""Generate least-privilege MariaDB grants for the audit table manifest.

Run as schema owner only after the implemented A tables have been migrated. Do not
execute this against production without a verified inventory of real tables.
"""

from __future__ import annotations

from tools.audit_enforcement import (
    APPEND_ONLY,
    INSERT_ONLY_HEADERS,
    MUTABLE,
    VERSIONED,
    quote_identifier,
)


def quote_sql_string(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("invalid SQL string")
    return "'" + value.replace("'", "''") + "'"


def runtime_grants(
    *, database: str, username: str, host: str, existing_tables: set[str]
) -> list[str]:
    """Explicit table grants; never use wildcard write privileges on A tables.

    Option A: protect existing A tables without inventing future feature tables.
    The two infrastructure tables must exist; unknown physical tables fail closed.
    """
    from tools.audit_enforcement import validate_manifest

    unknown, _ = validate_manifest(existing_tables - {"django_migrations"})
    if unknown:
        raise ValueError("unclassified tables in runtime grant inventory")
    missing = {"command_receipt", "audit_event"} - existing_tables
    if missing:
        raise ValueError("append-only tables not migrated: " + ", ".join(sorted(missing)))
    account = quote_sql_string(username) + "@" + quote_sql_string(host)
    db = quote_identifier(database)
    return [
        f"GRANT SELECT, INSERT ON {db}.{quote_identifier(table)} TO {account}"
        for table in sorted(APPEND_ONLY & existing_tables)
    ]


# Granted individually by the audit grant command (identity anchors have narrower grants).
IDENTITY_TABLES = frozenset({"organization", "user"})


def feature_table_grants(
    *, database: str, username: str, host: str, existing_tables: set[str]
) -> list[str]:
    """Grants on implemented V and M feature tables: no DELETE or DDL.

    V tables are inserted as new revisions; the runtime account cannot update or delete them
    (SPEC 17.7 item 1). Immutable M headers also receive no UPDATE. Only tables that exist
    are granted, so unmigrated features add nothing.
    """
    account = quote_sql_string(username) + "@" + quote_sql_string(host)
    db = quote_identifier(database)
    grants = []
    for table in sorted((VERSIONED | MUTABLE) & existing_tables - IDENTITY_TABLES):
        privileges = (
            "SELECT, INSERT, UPDATE" if table in MUTABLE - INSERT_ONLY_HEADERS else "SELECT, INSERT"
        )
        grants.append(f"GRANT {privileges} ON {db}.{quote_identifier(table)} TO {account}")
    return grants


def assert_runtime_table_privileges(
    actual: dict[str, set[str]], *, append_only: set[str] = APPEND_ONLY
) -> None:
    """Check effective TABLE privileges obtained from an actual DB connection.

    Caller must also check global/schema grants and roles, since table-level
    grants alone do not reveal inherited UPDATE/DELETE/DDL privileges.
    """
    forbidden = {
        "UPDATE",
        "DELETE",
        "ALTER",
        "DROP",
        "CREATE",
        "INDEX",
        "TRIGGER",
        "REFERENCES",
        "GRANT OPTION",
    }
    for table in sorted(append_only):
        privileges = {p.upper() for p in actual.get(table, set())}
        if not {"SELECT", "INSERT"} <= privileges:
            raise ValueError(f"{table}: SELECT/INSERT not both granted")
        bad = privileges & forbidden
        if bad:
            raise ValueError(f"{table}: forbidden privileges {sorted(bad)}")
