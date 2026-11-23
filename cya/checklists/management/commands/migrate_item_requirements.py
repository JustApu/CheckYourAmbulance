"""Move one organization to vehicle item requirements (SPEC 6.0.0 18.3, issue #256 C1).

Run once per organization, as an active Administrator, after its move to vehicle checklists
(migrate_vehicle_checklists). It is one audited ITEM_REQUIREMENT_CUTOVER command, all or
nothing: it creates one item requirement per stock from every published vehicle checklist
version and a source-row map for each item row, and from then on publishing a checklist
version links its rows to their requirements. It prints its operation id; rerunning it with
--operation-id and that id replays the first result, and any other run is refused once the
cutover is recorded.

--dry-run reads only: it prints what the migration would create and every problem that would
refuse it: a conflict, a possible move or split, or a deficiency whose place is ambiguous and not
yet recorded (assign_deficiency_place). It also lists every deficiency of an item kept in several
places, with whether its place is recorded.
"""

from uuid import UUID, uuid4

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization, User
from cya.checklists.item_requirement_cutover import (
    ItemRequirementMigrationError,
    migrate_item_requirements,
    plan_item_requirements,
)
from cya.core.audit_commands import CommandDenied


def _uuid(value, name):
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise CommandError(f"--{name} must be a UUID") from None


class Command(BaseCommand):
    help = "Migrate an organization to vehicle item requirements (SPEC 6.0.0 18.3)."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="organization UUID")
        parser.add_argument(
            "--actor-user-id", required=True, help="an active Administrator's user UUID"
        )
        parser.add_argument(
            "--operation-id",
            help="reuse the id a run printed to retry it after an interruption (default: new)",
        )
        parser.add_argument("--dry-run", action="store_true", help="plan without writing")

    def _report(self, counts):
        for name, value in counts.items():
            self.stdout.write(f"  {name.replace('_', ' ')}: {value}")

    def handle(self, *args, **options):
        org = _uuid(options["organization"], "organization")
        actor = _uuid(options["actor_user_id"], "actor-user-id")
        if not Organization.objects.filter(pk=org).exists():
            raise CommandError("unknown organization")
        if not User.objects.filter(pk=actor, organization_id=org).exists():
            raise CommandError("the actor is not a user of that organization")
        if options["dry_run"]:
            plan = plan_item_requirements(org)
            self.stdout.write("Dry run; nothing written. The migration would create:")
            self._report(plan.counts())
            for problem in plan.problems:
                self.stdout.write(f"  problem: {problem}")
            for deficiency in plan.ambiguous_deficiencies:
                self.stdout.write(f"  place needed: {deficiency}")
            if plan.problems:
                raise CommandError(f"{len(plan.problems)} problem(s) would refuse the migration")
            return
        operation = (
            _uuid(options["operation_id"], "operation-id")
            if options["operation_id"]
            else str(uuid4())
        )
        self.stdout.write(f"Operation id {operation} (pass it as --operation-id to retry this run)")
        try:
            result = migrate_item_requirements(
                organization_id=org, actor_id=actor, operation_id=operation
            )
        except CommandDenied:
            raise CommandError(
                "the actor may not run the migration (Administrators only)"
            ) from None
        except ItemRequirementMigrationError as error:
            raise CommandError(f"refused, nothing written: {error}") from None
        self.stdout.write(
            "Migrated to item requirements"
            + ("; replayed the earlier run" if result.replayed else "")
            + ":"
        )
        self._report({k: v for k, v in result.body.items() if k != "code"})
