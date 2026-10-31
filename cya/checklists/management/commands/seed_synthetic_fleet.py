"""Seed the synthetic sample EMS organization fleet and checklist (idempotent, --dry-run).

Writes only synthetic, non-authoritative data from tests/fixtures/demo-fleet.json
(SPEC CHK-7, OQ-01) and must never be used to load operational checklist data. The
checklist is drafted and published as an existing administrator, through the audited
commands:

    python manage.py seed_synthetic_fleet --actor-user-id <administrator user id> [--dry-run]

Run with the schema-owner account in a development or demonstration database.
"""

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization, User
from cya.authorization.backend import canonical_id
from cya.checklists.seed import plan_seed, seed_fleet
from cya.core.audit_commands import CommandDenied


class Command(BaseCommand):
    help = "Create the synthetic fleet and publish its synthetic checklist; safe to run again."

    def add_arguments(self, parser):
        parser.add_argument("--actor-user-id", required=True, help="Administrator user id.")
        parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")

    def handle(self, *args, actor_user_id, dry_run, **options):
        try:
            canonical_id(actor_user_id)
        except ValueError:
            raise CommandError("--actor-user-id must be a canonical UUID.") from None
        organizations = list(Organization.objects.filter(active=True)[:2])
        if len(organizations) != 1:
            raise CommandError("Exactly one active organization is required (create_organization).")
        organization = organizations[0]
        if not User.objects.filter(
            pk=actor_user_id, organization=organization, active=True
        ).exists():
            raise CommandError("--actor-user-id is not an active user of the organization.")
        plan = plan_seed(organization.pk)
        summary = (
            f"{plan.vehicles} vehicles, {plan.slots} slot rows, {plan.items} items, "
            f"checklist {'to publish' if plan.publishes else 'already published'}"
        )
        if dry_run:
            self.stdout.write(f"Dry run: would ensure {summary}.")
            return
        try:
            seed_fleet(organization_id=organization.pk, actor_id=actor_user_id)
        except CommandDenied:
            raise CommandError(
                "The actor is not permitted to draft and publish templates."
            ) from None
        self.stdout.write(f"Seeded synthetic data: {summary}.")
