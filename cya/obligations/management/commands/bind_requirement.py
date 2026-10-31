"""Operator transport for an administrator-attributed requirement binding."""

from datetime import datetime
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from cya.obligations.commands import BindingError, bind_requirement
from cya.obligations.models import BINDING_ACTIONS


class Command(BaseCommand):
    help = (
        "Append an administrator-authorized binding of a vehicle's checklist template to a "
        "schedule slot (or unbind it); never overwrites history."
    )

    def add_arguments(self, parser):
        for name in ("organization", "actor", "vehicle", "template", "slot", "operation-id"):
            parser.add_argument("--" + name, required=True)
        parser.add_argument("--action", choices=BINDING_ACTIONS, default="BIND")
        parser.add_argument("--effective-at", help="Aware ISO 8601 instant; default: now")

    def handle(self, *args, **options):
        try:
            effective_at = (
                datetime.fromisoformat(options["effective_at"]) if options["effective_at"] else None
            )
            attempt = str(uuid4())
            result = bind_requirement(
                organization_id=options["organization"],
                actor_id=options["actor"],
                vehicle_id=options["vehicle"],
                template_id=options["template"],
                slot=options["slot"],
                action=options["action"],
                effective_at=effective_at,
                operation_id=options["operation_id"],
                correlation_id=attempt,
                request_attempt_id=attempt,
            )
        except BindingError as exc:
            raise CommandError(f"Binding refused: {exc}") from exc
        except Exception as exc:
            raise CommandError(
                "Binding failed; inspect the authorized inputs and reuse the operation ID "
                "after an uncertain outcome"
            ) from exc
        if result.operation_reused:
            raise CommandError("Operation ID reused for different intent; no changes made")
        self.stdout.write("Unchanged: replayed." if result.replayed else "Binding committed.")
