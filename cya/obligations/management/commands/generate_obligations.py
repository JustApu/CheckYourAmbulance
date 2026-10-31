"""Materialize routine obligations ahead of time; safe to run as often as needed (TIME-1)."""

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization
from cya.authorization.backend import canonical_id
from cya.obligations.generation import (
    MAX_HORIZON_HOURS,
    MAX_LOOKBACK_HOURS,
    horizon_hours,
    lookback_hours,
    run_generation,
)


class Command(BaseCommand):
    help = (
        "Generate and reconcile obligations from --lookback-hours ago through --horizon-hours "
        "ahead for one organization. Idempotent: a run that finds everything current writes "
        "nothing, and a missed run is repaired by the next. Each unit is its own audited "
        "command. Exits non-zero if any unit failed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization UUID")
        parser.add_argument(
            "--horizon-hours",
            type=int,
            default=horizon_hours(),
            help=f"Hours ahead to materialize, 0-{MAX_HORIZON_HOURS} (default %(default)s)",
        )
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=lookback_hours(),
            help=f"Hours back to repair after a missed run, 0-{MAX_LOOKBACK_HOURS} "
            "(default %(default)s)",
        )
        parser.add_argument(
            "--vehicle", action="append", help="Limit to this vehicle UUID (repeatable)"
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be written; nothing is written, audited or reserved",
        )

    def handle(self, *args, organization, dry_run, **options):
        try:
            for vehicle_id in options["vehicle"] or []:
                canonical_id(vehicle_id)
            org = Organization.objects.get(pk=organization, active=True)
            summary = run_generation(
                str(org.pk),
                vehicle_ids=options["vehicle"],
                horizon=options["horizon_hours"],
                lookback=options["lookback_hours"],
                dry_run=dry_run,
            )
        except (Organization.DoesNotExist, ValueError) as exc:
            raise CommandError(f"Invalid organization or option: {exc}") from exc
        verb = "Would write" if dry_run else "Wrote"
        for result in summary.changed:
            c = result.counts
            self.stdout.write(
                f"{result.vehicle_id} {result.due_at.isoformat()}: {verb.lower()} "
                f"{c['selected']} selected, {c['replaced']} replaced, {c['retired']} retired, "
                f"{c['odometer']} odometer"
            )
        unchanged = sum(r.status in ("UNCHANGED", "REPLAYED") for r in summary.results)
        self.stdout.write(
            f"{verb}: {summary.total('selected')} selected, {summary.total('replaced')} replaced, "
            f"{summary.total('retired')} retired, {summary.total('odometer')} odometer "
            f"in {len(summary.changed)} unit(s); {unchanged} unit(s) already current; "
            f"{len(summary.failures)} failed."
        )
        for failure in summary.failures:
            self.stderr.write(
                f"FAILED {failure.vehicle_id} {failure.due_at.isoformat()}: {failure.error}"
            )
        if summary.failures:
            raise CommandError(f"{len(summary.failures)} unit(s) failed; rerun after fixing")
