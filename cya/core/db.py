"""Refuse database connections whose session settings are unsafe.

settings.DATABASE_INIT_COMMAND asks for these values on every connection, but
sql_mode and friends can be changed by any account and an init command can be
misconfigured, so each new connection is checked against the server's actual
values (SPEC 17.7 item 4; docs/data-model-contracts.md section 3). A mismatch
raises, so the application fails closed instead of writing with weak checks.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

REQUIRED_SQL_MODES = frozenset(
    {
        "STRICT_ALL_TABLES",
        "ERROR_FOR_DIVISION_BY_ZERO",
        "NO_ZERO_DATE",
        "NO_ZERO_IN_DATE",
        "NO_ENGINE_SUBSTITUTION",
    }
)
EXPECTED = {
    "time_zone": "+00:00",
    "foreign_key_checks": 1,
    "check_constraint_checks": 1,
    # MariaDB 10.11 names this tx_isolation (transaction_isolation is MySQL's).
    "tx_isolation": "READ-COMMITTED",
}
SETTINGS_QUERY = (
    "SELECT @@session.time_zone, @@session.sql_mode, @@session.foreign_key_checks,"
    " @@session.check_constraint_checks, @@session.tx_isolation"
)


def session_problems(row):
    """Return a list of problems for one SETTINGS_QUERY result row."""
    if row is None or len(row) != 5:
        return [f"unexpected session settings result {row!r}"]
    time_zone, sql_mode, fk_checks, check_checks, isolation = row
    actual = {
        "time_zone": time_zone,
        "foreign_key_checks": fk_checks,
        "check_constraint_checks": check_checks,
        "tx_isolation": isolation,
    }
    problems = [
        f"{name} is {actual[name]!r}, expected {expected!r}"
        for name, expected in EXPECTED.items()
        if actual[name] != expected
    ]
    modes = set(sql_mode.split(",")) if isinstance(sql_mode, str) and sql_mode else set()
    missing = sorted(REQUIRED_SQL_MODES - modes)
    if missing:
        problems.append(f"sql_mode {sql_mode!r} lacks {', '.join(missing)}")
    return problems


def verify_session_settings(sender, connection, **kwargs):
    """connection_created handler: raise if the new session is not safe.

    Django has already stored the new connection when this runs. On every
    unsuccessful path (unsafe values, or the query itself failing) it is
    closed, so the next ensure_connection() reconnects and verifies again
    instead of reusing an unchecked session.
    """
    verified = False
    try:
        if connection.vendor != "mysql":
            raise ImproperlyConfigured(f"Unsupported database vendor {connection.vendor!r}.")
        with connection.cursor() as cursor:
            cursor.execute(SETTINGS_QUERY)
            row = cursor.fetchone()
        problems = session_problems(row)
        if problems:
            raise ImproperlyConfigured(
                f"Database session for alias {connection.alias!r} is unsafe: " + "; ".join(problems)
            )
        if settings.CYA_DATABASE_ROLE == "runtime":
            from tools.audit_runtime_verify import check_runtime_connection

            try:
                with connection.cursor() as cursor:
                    check_runtime_connection(
                        database=connection.settings_dict["NAME"], cursor=cursor
                    )
            except ValueError as exc:
                raise ImproperlyConfigured("Database runtime privileges are unsafe") from exc
        verified = True
    finally:
        if not verified:
            # The original exception propagates; close() errors are secondary.
            try:
                connection.close()
            except Exception:
                pass
