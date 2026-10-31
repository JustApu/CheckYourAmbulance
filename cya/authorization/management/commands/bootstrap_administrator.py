"""SPEC 3.6 / OQ-36: operator-only first-administrator bootstrap."""

from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cya.accounts.models import Organization, User
from cya.authorization.backend import backend_call, canonical_id, get_backend, load_state
from cya.authorization.lifecycle import BootstrapRefused, plan_first_administrator
from cya.authorization.locking import locked_identities
from cya.authorization.persistence import record_bootstrap_failure


class Command(BaseCommand):
    help = "Seed the first Administrator for an active provisioned crew member; never a web action."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="Organization UUID")
        parser.add_argument("--user", required=True, help="Existing provisioned user UUID")
        parser.add_argument(
            "--dry-run", action="store_true", help="Validate and preview; write nothing"
        )

    def handle(self, *args, organization, user, dry_run, **options):
        try:
            organization_id, user_id = canonical_id(organization), canonical_id(user)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        correlation_id = str(uuid4())
        try:
            self.execute_bootstrap(organization_id, user_id, dry_run, correlation_id)
        except CommandError as exc:
            # No invented tenant for invalid/missing organization IDs. Audit
            # eligible operator attempts only; --dry-run remains write-free.
            if not dry_run:
                try:
                    if Organization.objects.filter(pk=organization_id).exists():
                        record_bootstrap_failure(
                            organization_id,
                            user_id,
                            correlation_id,
                            error=exc,
                        )
                except Exception:
                    raise CommandError(
                        "Bootstrap unavailable; mandatory audit unavailable"
                    ) from None
            raise
        self.stdout.write(
            "Dry run: would grant the first Administrator; no writes performed."
            if dry_run
            else "First Administrator grant completed."
        )

    def execute_bootstrap(self, organization_id, user_id, dry_run, correlation_id):
        try:
            backend = get_backend()
            with (
                transaction.atomic(durable=True),
                locked_identities(organization_id, [user_id]) as (org, users),
            ):
                target = users[user_id]
                if not org.active:
                    raise BootstrapRefused("Organization must be active")
                change = plan_first_administrator(
                    load_state(backend, target),
                    target_active=target.active,
                    has_administrator_history=backend_call(
                        backend, "has_administrator_history", organization_id=organization_id
                    ),
                )
                if not dry_run:
                    backend_call(
                        backend,
                        "append_role_change",
                        user=target,
                        change=change,
                        correlation_id=correlation_id,
                        origin="OPERATOR_BOOTSTRAP",
                    )
        except (Organization.DoesNotExist, User.DoesNotExist):
            raise CommandError("Organization/user pair does not exist") from None
        except BootstrapRefused as exc:
            raise CommandError(str(exc)) from exc
        except Exception:
            # No raw adapter/DB exception text: it can contain credentials or PII.
            raise CommandError("Bootstrap unavailable; no successful grant is confirmed") from None
