"""Create the single R1 organization row, once (idempotent, with --dry-run).

Sign-in requires exactly one active organization. Run with the schema-owner
account during setup:

    python manage.py create_organization --name "Sample Agency" --time-zone America/New_York
"""

import zoneinfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from cya.accounts.models import Organization
from cya.deficiencies.severity import ensure_default_severities


class Command(BaseCommand):
    help = "Create the organization if none exists; otherwise report it and change nothing."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--time-zone", required=True, help="IANA zone, e.g. America/New_York")
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    def handle(self, *args, name, time_zone, dry_run, **options):
        name = name.strip()
        if not name or len(name) > 200:
            raise CommandError("--name must be 1 to 200 characters.")
        if time_zone not in zoneinfo.available_timezones():
            raise CommandError(f"--time-zone {time_zone!r} is not an IANA time zone.")
        with transaction.atomic():
            existing = list(Organization.objects.select_for_update().order_by("id")[:2])
            if existing:
                names = ", ".join(f"{o.name} ({o.id})" for o in existing)
                self.stdout.write(f"Unchanged: organization already exists: {names}")
                return
            if dry_run:
                self.stdout.write(f"Dry run: would create organization {name!r} ({time_zone}).")
                return
            organization = Organization.objects.create(name=name, station_zone=time_zone)
            # Deficiency severity levels are data; start the organization with the defaults.
            ensure_default_severities(organization.pk, timezone.now())
        self.stdout.write(f"Created organization {organization.name!r} ({organization.id}).")
