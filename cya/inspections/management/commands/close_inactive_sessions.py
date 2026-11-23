"""End inspection sessions at their end instant (SPEC 6.3 SES-1, SES-8, SES-9).

Safe to run on a schedule.
"""

from django.core.management.base import BaseCommand

from cya.inspections.commands import auto_close_inactive_sessions, due_sessions


class Command(BaseCommand):
    help = (
        "End Open inspection sessions at their end instant: the last persisted activity plus six "
        "hours, or the session's resume deadline when that comes first. Each ending is stamped at "
        "that instant, not when the job runs. Idempotent; reports closed (and of those, ended at "
        "the deadline), skipped and failed counts. --dry-run lists what is due and changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200, help="Most sessions per run.")
        parser.add_argument(
            "--dry-run", action="store_true", help="List the sessions due; write nothing."
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            due = due_sessions(limit=options["limit"])
            for _org, session_id, ends_at in due:
                self.stdout.write(f"due session={session_id} ends_at={ends_at.isoformat()}")
            self.stdout.write(f"due={len(due)}")
            return
        counts = auto_close_inactive_sessions(limit=options["limit"])
        self.stdout.write(
            "closed={closed} at_deadline={at_deadline} skipped={skipped} failed={failed}".format(
                **counts
            )
        )
        if counts["failed"]:
            raise SystemExit(1)
