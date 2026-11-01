"""Run one bounded deadline-overdue notice pass for every active organization (cron or timer)."""

from django.core.management.base import BaseCommand

from cya.accounts.models import Organization
from cya.deficiencies.overdue import notify_overdue


class Command(BaseCommand):
    help = "Notify each newly overdue deficiency once per governing deadline (issue 14)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        for org in Organization.objects.filter(active=True).order_by("id"):
            report = notify_overdue(str(org.pk), limit=options["limit"])
            self.stdout.write(
                f"{org.pk}: notified={report.notified} already={report.already} "
                f"suppressed={report.suppressed} skipped={report.skipped} errors={report.errors}"
            )
