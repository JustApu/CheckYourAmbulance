"""Explicit schema-owner setup, with a read-only preview."""

from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization
from cya.schedules.commands import DEFAULT_SCHEDULES, seed_default_schedules
from cya.schedules.engine import Rule, utc_instant


class Command(BaseCommand):
    help = "Seed the illustrative SPEC 5.2 schedules once for an explicit organization (operator setup)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True)
        parser.add_argument("--effective-at", required=True, help="Aware ISO 8601 instant")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, organization, effective_at, dry_run, **options):
        if settings.CYA_DATABASE_ROLE != "schema_owner":
            raise CommandError("Setup requires schema_owner mode")
        try:
            instant = utc_instant(datetime.fromisoformat(effective_at))
            org = Organization.objects.get(pk=organization, active=True)
            for _, recurrence, wall in DEFAULT_SCHEDULES:
                Rule(org.station_zone, wall, instant, recurrence)
            if dry_run:
                self.stdout.write(
                    "Dry run: six initial schedule revisions; no writes or audit effects."
                )
                return
            result = seed_default_schedules(str(org.pk), instant)
            if result.operation_reused:
                raise CommandError("Already seeded with different parameters; no changes made")
        except (ValueError, KeyError, Organization.DoesNotExist) as exc:
            raise CommandError("Invalid organization, zone or effective instant") from exc
        self.stdout.write(
            "Unchanged: seed already committed."
            if result.replayed
            else "Created six schedule revisions."
        )
