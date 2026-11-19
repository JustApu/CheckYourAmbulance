"""Run one bounded check-due notice pass for every active organization (cron or timer, issue #202)."""

from django.core.management.base import BaseCommand

from cya.accounts.models import Organization
from cya.operations.check_due import notify_check_due


class Command(BaseCommand):
    help = "Notify each vehicle crew once per due instant when checks come due (issue #202)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        for org in Organization.objects.filter(active=True).order_by("id"):
            report = notify_check_due(str(org.pk), limit=options["limit"])
            self.stdout.write(
                f"{org.pk}: notified={report.notified} already={report.already} "
                f"out_of_service={report.out_of_service} errors={report.errors}"
            )
