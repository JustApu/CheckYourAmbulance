from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from tools.audit_runtime_verify import check_runtime_connection


class Command(BaseCommand):
    help = "Read-only verification using the application's distinct runtime credentials."

    def handle(self, *args, **options):
        if settings.CYA_DATABASE_ROLE != "runtime":
            raise CommandError("Use runtime mode and distinct runtime credentials")
        try:
            with connection.cursor() as cursor:
                check_runtime_connection(database=connection.settings_dict["NAME"], cursor=cursor)
        except Exception as exc:
            raise CommandError("Runtime privilege verification failed") from exc
        self.stdout.write(
            "Runtime privileges verified; schema guards require separate owner inspection."
        )
