"""Close inspection sessions idle for six hours (SPEC 6.3). Safe to run on a schedule."""

from django.core.management.base import BaseCommand

from cya.inspections.commands import auto_close_inactive_sessions


class Command(BaseCommand):
    help = (
        "Close Open inspection sessions with no persisted activity for six hours, stamped at the "
        "last activity plus six hours. Idempotent; reports closed, skipped and failed counts."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200, help="Most sessions per run.")

    def handle(self, *args, **options):
        counts = auto_close_inactive_sessions(limit=options["limit"])
        self.stdout.write("closed={closed} skipped={skipped} failed={failed}".format(**counts))
        if counts["failed"]:
            raise SystemExit(1)
