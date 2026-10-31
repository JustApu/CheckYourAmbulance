from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from cya.core.audit_schema import inspect_audit_guards, inspect_command_receipt_guards


class Command(BaseCommand):
    help = "Read-only schema-owner verification of classified tables, append-only guards, and receipt insert guards."

    def handle(self, *args, **options):
        if settings.CYA_DATABASE_ROLE != "schema_owner":
            raise CommandError("Schema inspection requires explicit schema_owner mode")
        try:
            with connection.cursor() as cursor:
                guarded = inspect_audit_guards(cursor)
                cmd_guarded = inspect_command_receipt_guards(cursor)
        except Exception as exc:
            raise CommandError("Audit schema verification failed") from exc
        self.stdout.write(
            f"Verified {len(guarded)} existing append-only tables and {len(cmd_guarded)} command-guarded tables; future tables are not created."
        )
