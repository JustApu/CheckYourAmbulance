"""Schema-owner/DBA grant installation, after migrations and account creation."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from cya.core.audit_schema import inspect_audit_guards
from tools.audit_grants import feature_table_grants, runtime_grants
from tools.audit_runtime_verify import validate_runtime_grants


class Command(BaseCommand):
    help = "Grant an existing runtime account narrow access to the guarded physical schema."

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True)
        parser.add_argument("--host", required=True)
        parser.add_argument(
            "--dry-run", action="store_true", help="Validate and print SQL; do not grant"
        )

    def handle(self, *args, user, host, dry_run, **options):
        if settings.CYA_DATABASE_ROLE != "schema_owner":
            raise CommandError("Grant installation requires explicit schema_owner mode")
        database = connection.settings_dict["NAME"]
        try:
            with connection.cursor() as cursor:
                present = inspect_audit_guards(cursor)
                # Refuse a missing account, roles or pre-existing broad powers.
                # Never rely on GRANT's possible implicit account creation.
                cursor.execute("SHOW GRANTS FOR %s@%s", [user, host])
                existing = [row[0] for row in cursor.fetchall()]
                validate_runtime_grants(existing, database=database, required_tables=set())
                statements = runtime_grants(
                    database=database,
                    username=user,
                    host=host,
                    existing_tables=present | {"organization", "user"},
                )
                from tools.audit_grants import quote_sql_string

                cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"
                )
                statements += feature_table_grants(
                    database=database,
                    username=user,
                    host=host,
                    existing_tables={row[0] for row in cursor.fetchall()},
                )

                account = quote_sql_string(user) + "@" + quote_sql_string(host)
                statements += [
                    f"GRANT SELECT ON `{database}`.`organization` TO {account}",
                    f"GRANT SELECT, INSERT, UPDATE ON `{database}`.`user` TO {account}",
                ]
                cursor.execute(
                    "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='schedule_rule'"
                )
                if cursor.fetchone():
                    statements.append(
                        f"GRANT SELECT, INSERT ON `{database}`.`schedule_rule` TO {account}"
                    )
                if dry_run:
                    self.stdout.write("\n".join(sql + ";" for sql in statements))
                    return
                # MariaDB GRANT auto-commits. Partial failure is safe to inspect
                # and rerun; this command never revokes privileges or touches data.
                for sql in statements:
                    cursor.execute(sql)
                cursor.execute("SHOW GRANTS FOR %s@%s", [user, host])
                validate_runtime_grants(
                    [row[0] for row in cursor.fetchall()],
                    database=database,
                    required_tables=present,
                )
        except Exception as exc:
            raise CommandError(
                "Grant configuration failed; inspect account/schema and rerun"
            ) from exc
        self.stdout.write("Narrow grants installed. Verify a new connection in runtime mode.")
