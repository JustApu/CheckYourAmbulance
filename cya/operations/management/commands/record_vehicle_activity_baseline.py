"""Give every vehicle without an activity history its baseline event (SPEC 4.8 CFG-1).

Run once per organization when deploying issue #130 part 2, before anyone deactivates or
reactivates a vehicle. It is one audited VEHICLE_ACTIVITY_BASELINE command per organization,
attributed to a service identity, with a fixed operation id: rerunning it replays the first
result and writes nothing. --dry-run counts what would be written without writing.
"""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.operations.models import VehicleActivityEvent
from cya.operations.vehicles import record_activity_baseline


class Command(BaseCommand):
    help = "Record each vehicle's current active flag as its activity baseline (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="organization UUID")
        parser.add_argument("--dry-run", action="store_true", help="count without writing")

    def handle(self, *args, **options):
        try:
            org = str(UUID(options["organization"]))
        except ValueError:
            raise CommandError("--organization must be a UUID") from None
        if not Organization.objects.filter(pk=org).exists():
            raise CommandError("unknown organization")
        if options["dry_run"]:
            pending = Vehicle.objects.filter(organization_id=org).exclude(
                pk__in=VehicleActivityEvent.objects.filter(organization_id=org).values("vehicle_id")
            )
            self.stdout.write(
                f"Would record baselines: {pending.count()} vehicles "
                f"({pending.filter(active=False).count()} inactive)"
            )
            return
        result = record_activity_baseline(org)
        body = result.body
        self.stdout.write(
            f"Baselines recorded: {body['vehicles']} vehicles ({body['inactive']} inactive)"
            + ("; replayed the earlier run" if result.replayed else "")
        )
