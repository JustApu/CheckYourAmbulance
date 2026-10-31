"""Operator transport for an administrator-attributed schedule revision."""

from datetime import date, datetime, time
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from cya.schedules.commands import publish_rule
from cya.schedules.engine import RECURRENCES, Rule


class Command(BaseCommand):
    help = "Append an administrator-authorized schedule revision; never overwrite history."

    def add_arguments(self, parser):
        for name in (
            "organization",
            "actor",
            "slot",
            "zone",
            "wall-time",
            "effective-at",
            "operation-id",
        ):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--recurrence", choices=RECURRENCES, required=True)
        parser.add_argument("--early-seconds", type=int, default=3600)
        parser.add_argument("--grace-seconds", type=int, default=3600)
        parser.add_argument("--interval-days", type=int)
        parser.add_argument("--anchor-date")

    def handle(self, *args, **options):
        try:
            rule = Rule(
                zone=options["zone"],
                wall_time=time.fromisoformat(options["wall_time"]),
                effective_at=datetime.fromisoformat(options["effective_at"]),
                recurrence=options["recurrence"],
                early_seconds=options["early_seconds"],
                grace_seconds=options["grace_seconds"],
                interval_days=options["interval_days"],
                anchor_date=date.fromisoformat(options["anchor_date"])
                if options["anchor_date"]
                else None,
            )
            attempt = str(uuid4())
            result = publish_rule(
                organization_id=options["organization"],
                actor_id=options["actor"],
                slot=options["slot"],
                rule=rule,
                operation_id=options["operation_id"],
                correlation_id=attempt,
                request_attempt_id=attempt,
            )
            if result.operation_reused:
                raise CommandError("Operation ID reused for different intent; no changes made")
        except Exception as exc:
            raise CommandError(
                "Schedule publish failed; inspect authorized inputs and reuse the operation ID after an uncertain outcome"
            ) from exc
        self.stdout.write(
            "Unchanged: replayed." if result.replayed else "Schedule revision committed."
        )
