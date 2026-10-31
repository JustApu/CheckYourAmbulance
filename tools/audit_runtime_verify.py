"""Fail-closed runtime privilege inspection for MariaDB 10.11.

The check runs as the *runtime* account: SHOW GRANTS FOR CURRENT_USER
reports direct global/schema/table grants and assigned roles. Roles are not
expanded here: reject assigned or active roles rather than trusting
INFORMATION_SCHEMA.TABLE_PRIVILEGES alone.
"""

from __future__ import annotations

import re

from django.db import connection

from tools.audit_enforcement import (
    APPEND_ONLY,
    INSERT_ONLY_HEADERS,
    MUTABLE,
    VERSIONED,
    quote_identifier,
)


def validate_runtime_grants(
    grant_lines: list[str],
    *,
    database: str,
    required_tables=frozenset({"command_receipt", "audit_event"}),
) -> None:
    """Reject broad permissions and role grants; require A-table SELECT/INSERT.

    Only exact table-level grants are accepted for append-only tables.
    The account may hold additional grants for M/V tables; those grants must
    never be global or database-wide.
    """
    if not grant_lines:
        raise ValueError("runtime grant inventory is empty")
    allowed = set()
    quote_identifier(database)
    for line in grant_lines:
        upper = line.upper()
        if "WITH GRANT OPTION" in upper:
            raise ValueError("runtime grant delegation is forbidden")
        if not upper.startswith("GRANT ") or " ON " not in upper:
            # MariaDB can emit a role grant without ON; do not accept it.
            raise ValueError("unexpanded runtime role or unexpected grant")
        match = re.fullmatch(
            r"GRANT (.+?) ON (.+?) TO (.+?)(?: WITH .+)?", line, flags=re.IGNORECASE
        )
        if match is None:
            raise ValueError("unparseable runtime grant")
        privileges, scope, _principal = match.groups()
        names = {name.strip().upper() for name in privileges.split(",")}
        if scope.strip() in ("*.*", f"`{database}`.*", f"{database}.*"):
            if names != {"USAGE"}:
                raise ValueError("broad runtime grant")
            continue
        prefix = f"`{database}`."
        if not scope.startswith(prefix):
            raise ValueError("unexpected database in runtime grants")
        table = scope[len(prefix) :].strip("`")
        limits = {"SELECT", "INSERT"}
        if table in MUTABLE - INSERT_ONLY_HEADERS:
            limits.add("UPDATE")
        if table not in APPEND_ONLY | MUTABLE | VERSIONED or not names <= limits:
            raise ValueError("forbidden or unclassified runtime table privilege")
        if table in APPEND_ONLY:
            if not names <= {"SELECT", "INSERT"}:
                raise ValueError("invalid append-only table privileges")
            allowed.update((table, name) for name in names)
    missing = {
        (table, privilege) for table in required_tables for privilege in ("SELECT", "INSERT")
    } - allowed
    if missing:
        raise ValueError("missing required runtime A-table grants: " + repr(sorted(missing)))


def check_runtime_connection(
    *, database: str, cursor=None, required_tables=frozenset({"command_receipt", "audit_event"})
) -> None:
    """Check the current MariaDB connection; never use schema-owner credentials."""
    if cursor is None:
        with connection.cursor() as owned:
            return check_runtime_connection(
                database=database, cursor=owned, required_tables=required_tables
            )
    else:
        cursor.execute("SELECT CURRENT_ROLE()")
        if cursor.fetchone()[0] not in (None, "NULL"):
            raise ValueError("runtime roles must be disabled")
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'"
        )
        required_tables = set(required_tables) | (
            {row[0] for row in cursor.fetchall()} & APPEND_ONLY
        )
        cursor.execute("SHOW GRANTS FOR CURRENT_USER")
        validate_runtime_grants(
            [row[0] for row in cursor.fetchall()],
            database=database,
            required_tables=required_tables,
        )
