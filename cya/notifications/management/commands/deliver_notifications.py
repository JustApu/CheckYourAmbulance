"""Run one bounded email delivery pass for every active organization (cron or systemd timer)."""

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization
from cya.notifications.delivery import deliver_pending
from cya.notifications.senders import configured_sender


class Command(BaseCommand):
    help = "Send due notification emails once; failures are retried by the next run."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        sender = configured_sender()
        if sender is None:
            raise CommandError("CYA_NOTIFICATION_SENDER is not configured; email stays queued.")
        for org in Organization.objects.filter(active=True).order_by("id"):
            report = deliver_pending(str(org.pk), sender, limit=options["limit"])
            self.stdout.write(
                f"{org.pk}: sent={report.sent} retrying={report.retrying} "
                f"gave_up={report.gave_up} collisions={report.collisions} errors={report.errors}"
            )
