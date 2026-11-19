"""Run one bounded email delivery pass for every active organization (cron or systemd timer).

Each organization's email goes through its Outgoing-mail relay (SPEC DEP-13, issue #224), unless
CYA_NOTIFICATION_SENDER names an override. An organization with no relay is skipped and its email
stays queued; that is reported, not a failure, so the timer does not fail every minute.
"""

from django.core.management.base import BaseCommand

from cya.accounts.models import Organization
from cya.notifications.delivery import deliver_pending
from cya.notifications.senders import sender_for


class Command(BaseCommand):
    help = "Send due notification emails once; failures are retried by the next run."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        for org in Organization.objects.filter(active=True).order_by("id"):
            sender = sender_for(str(org.pk))
            if sender is None:
                self.stdout.write(
                    f"{org.pk}: no outgoing-mail relay configured; email stays queued"
                )
                continue
            report = deliver_pending(str(org.pk), sender, limit=options["limit"])
            self.stdout.write(
                f"{org.pk}: sent={report.sent} retrying={report.retrying} "
                f"gave_up={report.gave_up} collisions={report.collisions} errors={report.errors}"
            )
