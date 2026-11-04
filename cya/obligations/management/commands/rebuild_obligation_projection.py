"""Rebuild/compare disposable obligation evidence without changing immutable events."""

from dataclasses import replace
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from cya.accounts.models import Organization
from cya.checklists.models import Vehicle
from cya.obligations.current import evaluate_facts, refresh_obligations
from cya.obligations.models import Obligation, ObligationProjection
from cya.obligations.projections import get_vehicle_obligations_as_of


class Command(BaseCommand):
    help = "Rebuild current obligation projections from events; compare without writes with --dry-run or --verify."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True)
        parser.add_argument("--vehicle")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report differences without writing; nonzero on differences.",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Compare only; nonzero on missing/stale rows or replay mismatch.",
        )

    def handle(self, *args, **options):
        org = self._uuid(options["organization"])
        if not Organization.objects.filter(pk=org).exists():
            raise CommandError("organization not found")
        sources = Obligation.objects.filter(organization_id=org)
        if options["vehicle"]:
            vehicle = self._uuid(options["vehicle"])
            if not Vehicle.objects.filter(pk=vehicle, organization_id=org).exists():
                raise CommandError("vehicle not found in organization")
            sources = sources.filter(vehicle_id=vehicle)
        read_only = options["dry_run"] or options["verify"]
        different = verified = 0
        # The immutable source catalogue is streamed, one (vehicle,D) at a time. A group is
        # atomic; a failure leaves earlier groups repairable/repeatable on the next invocation.
        groups = (
            sources.order_by("vehicle_id", "due_at").values_list("vehicle_id", "due_at").distinct()
        )
        for vehicle_id, due in groups.iterator(chunk_size=250):
            ids = list(
                sources.filter(vehicle_id=vehicle_id, due_at=due).values_list("pk", flat=True)
            )
            with transaction.atomic():
                changed = refresh_obligations(org, ids, dry_run=read_only)
                different += len(changed)
                if not read_only:
                    if refresh_obligations(org, ids, dry_run=True):
                        raise CommandError("projection differs after rebuild; group rolled back")
                if not changed or not read_only:
                    self._verify_replay(org, str(vehicle_id), due, ids)
                    verified += len(ids)
        self.stdout.write(
            f"Different: {different}; verified: {verified}; writes: {'none' if read_only else 'projection only'}"
        )
        if read_only and different:
            raise CommandError("projection differences found; rebuild required")

    @staticmethod
    def _uuid(value):
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CommandError("organization and vehicle must be canonical UUIDs") from None
        return value

    @staticmethod
    def _verify_replay(org, vehicle, due, ids):
        at = timezone.now()
        # Independent selection/history loading, chain replay and full vehicle evaluator.
        replayed = get_vehicle_obligations_as_of(
            vehicle, at, organization_id=org, start=due, end=due
        )
        expected = {s.obligation.id: replace(s, attention=None, deficiencies=()) for s in replayed}
        actual = {
            row.obligation_id: evaluate_facts(row.facts, at)
            for row in ObligationProjection.objects.filter(
                organization_id=org,
                obligation_id__in=ids,
                selected=True,
            )
        }
        if actual != expected:
            raise CommandError("projection/event replay mismatch; rebuild group rolled back")
