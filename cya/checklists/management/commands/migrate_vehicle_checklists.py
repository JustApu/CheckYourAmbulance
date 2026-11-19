"""Move one organization to vehicle-owned checklists (SPEC 5.0.0 18.2, issue #220).

Run once per organization, as an active Administrator, after the vehicle checklist screens
(issue #220 part 3) are deployed: after the cutover the legacy template screens accept no
change. It is one audited CHECKLIST_CUTOVER command, all or nothing. Before it commits it checks
that every future due instant resolves to the same requirements as before; any difference
refuses it and nothing is written. It prints its operation id; rerunning it with
--operation-id and that id (after an interruption, say) replays the first result, and any
other run is refused once the cutover is recorded.

--dry-run reads only: it prints what the migration would create and every problem that would
refuse it. The equivalence check needs the writes, so it runs only for real.
"""

from uuid import UUID, uuid4

from django.core.management.base import BaseCommand, CommandError

from cya.accounts.models import Organization, User
from cya.checklists.cutover import CutoverError, migrate_to_vehicle_checklists, plan_cutover
from cya.core.audit_commands import CommandDenied


def _uuid(value, name):
    try:
        return str(UUID(value))
    except (TypeError, ValueError):
        raise CommandError(f"--{name} must be a UUID") from None


class Command(BaseCommand):
    help = "Migrate an organization's checklists to vehicle-owned checklists (SPEC 5.0.0 18.2)."

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
            plan = plan_cutover(org)
            self.stdout.write("Dry run; nothing written. The migration would create:")
            self._report(plan.counts())
            for problem in plan.problems:
                self.stdout.write(f"  problem: {problem}")
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
            result = migrate_to_vehicle_checklists(
                organization_id=org, actor_id=actor, operation_id=operation
            )
        except CommandDenied:
            raise CommandError(
                "the actor may not run the migration (Administrators only)"
            ) from None
        except CutoverError as error:
            raise CommandError(f"refused, nothing written: {error}") from None
        self.stdout.write(
            "Migrated to vehicle checklists"
            + ("; replayed the earlier run" if result.replayed else "")
            + ":"
        )
        self._report({k: v for k, v in result.body.items() if k != "code"})
