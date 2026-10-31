"""Real MariaDB lifecycle, replay, denial and last-admin concurrency tests."""

import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    DatabaseError,
    OperationalError,
    close_old_connections,
    connection,
    transaction,
)
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase

from cya.accounts import signin
from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role, set_user_active
from cya.authorization.guard import require_permission
from cya.authorization.lifecycle import LastAdministrator, current_roles
from cya.authorization.models import RoleGrant
from cya.authorization.persistence import DatabaseRoleBackend, current_role_codes
from cya.authorization.requests import permission_required
from cya.authorization.roles import grant_initial_crew
from cya.core.audit_commands import CommandDenied, OperationConflict
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.core.models import AuditEvent, CommandReceipt
from cya.integrations import VerifiedIdentity


class PersistentSetup:
    def setUp(self):
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.admin = self.member("admin")
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.admin.pk,
            stdout=io.StringIO(),
        )
        self.other = self.member("other")

    def member(self, subject):
        with transaction.atomic():
            user = User.objects.create(
                organization=self.org,
                idp_issuer="https://identity.example.test",
                idp_subject=subject,
                email=f"{subject}@example.test",
            )
            grant_initial_crew(user=user, command_id=str(uuid4()))
        return user

    def context(self, *, actor=None, target=None, operation=None):
        return dict(
            organization_id=self.org.pk,
            actor_id=(actor or self.admin).pk,
            target_id=(target or self.other).pk,
            operation_id=operation or str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
        )


class PersistentRoleTests(PersistentSetup, TestCase):
    def test_initial_and_bootstrap_receipts_audit_and_contract_role_codes(self):
        for user, expected in ((self.admin, ("ADMINISTRATOR", "CREW")), (self.other, ("CREW",))):
            self.assertEqual(current_role_codes(user), expected)
            state = current_roles(
                DatabaseRoleBackend().role_events(organization_id=self.org.pk, user_id=user.pk),
                organization_id=self.org.pk,
                user_id=user.pk,
            )
            self.assertEqual(set(state.grants), set(expected))
        for grant in RoleGrant.objects.select_related("command"):
            self.assertEqual(grant.recorded_at, grant.command.recorded_at)
            self.assertEqual(grant.command.actor_user_id, grant.actor_user_id)
            self.assertTrue(
                AuditEvent.objects.filter(command=grant.command, outcome="SUCCEEDED").exists()
            )

    def test_bootstrap_repeat_and_dry_run_write_nothing(self):
        counts = (
            RoleGrant.objects.count(),
            CommandReceipt.objects.count(),
            AuditEvent.objects.count(),
        )
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_administrator",
                organization=self.org.pk,
                user=self.other.pk,
                dry_run=True,
                stdout=io.StringIO(),
            )
        self.assertEqual(
            counts,
            (RoleGrant.objects.count(), CommandReceipt.objects.count(), AuditEvent.objects.count()),
        )

    def test_eligible_dry_run_preserves_all_persisted_counts(self):
        self.org = Organization.objects.create(name="New Sample Agency", station_zone="UTC")
        user = self.member("eligible")
        counts = (
            RoleGrant.objects.count(),
            CommandReceipt.objects.count(),
            AuditEvent.objects.count(),
        )
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=user.pk,
            dry_run=True,
            stdout=io.StringIO(),
        )
        self.assertEqual(
            counts,
            (RoleGrant.objects.count(), CommandReceipt.objects.count(), AuditEvent.objects.count()),
        )
        self.assertEqual(current_role_codes(user), ("CREW",))

    def test_signin_real_adapters_commit_and_sink_failure_rolls_back_everything(self):
        identity = VerifiedIdentity(
            "https://identity.example.test", "new", "new@example.test", "New Member", "example.test"
        )
        user = signin._find_or_provision(identity, self.org, str(uuid4()), None)
        self.assertEqual(current_role_codes(user), ("CREW",))
        self.assertEqual(
            set(AuditEvent.objects.filter(actor_user=user).values_list("action", flat=True)),
            {"USER_PROVISIONED", "SIGN_IN"},
        )
        counts = (
            User.objects.count(),
            RoleGrant.objects.count(),
            CommandReceipt.objects.count(),
            AuditEvent.objects.count(),
        )
        identity = VerifiedIdentity(
            identity.issuer, "failed", "failed@example.test", "Failed", "example.test"
        )
        with patch(
            "cya.core.auth_audit.append_audit", side_effect=RuntimeError("synthetic failure")
        ):
            with self.assertRaises(AuditPersistenceFailure):
                signin._find_or_provision(identity, self.org, str(uuid4()), None)
        self.assertEqual(
            counts,
            (
                User.objects.count(),
                RoleGrant.objects.count(),
                CommandReceipt.objects.count(),
                AuditEvent.objects.count(),
            ),
        )


class RoleCommandTests(PersistentSetup, TransactionTestCase):
    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def change(
        self, *, actor=None, target=None, role="ADMINISTRATOR", action="GRANT", operation=None
    ):
        return change_role(
            role=role,
            action=action,
            **self.context(actor=actor, target=target, operation=operation),
        )

    def test_grant_revoke_regrant_and_idempotent_replay(self):
        op = str(uuid4())
        first = self.change(role="SUPERVISOR", operation=op)
        count = RoleGrant.objects.count()
        replay = self.change(role="SUPPLY_OFFICER", operation=op)
        self.assertTrue(replay.replayed)
        self.assertTrue(replay.operation_reused)
        self.assertEqual(first.body, replay.body)
        self.assertEqual(RoleGrant.objects.count(), count)
        event = RoleGrant.objects.get(user=self.other, role="SUPERVISOR", action="GRANT")
        self.assertEqual(event.actor_user_id, self.admin.pk)
        self.assertEqual(event.actor_roles_at_time, ["ADMINISTRATOR", "CREW"])
        self.assertEqual(
            AuditEvent.objects.get(command=event.command).roles_at_time, event.actor_roles_at_time
        )
        self.change(role="SUPERVISOR", action="REVOKE")
        self.assertEqual(current_role_codes(self.other), ("CREW",))
        self.change(role="SUPERVISOR")
        self.assertEqual(current_role_codes(self.other), ("CREW", "SUPERVISOR"))
        count = RoleGrant.objects.count()
        self.change(role="SUPERVISOR")
        self.assertEqual(RoleGrant.objects.count(), count)

    def test_denied_actor_cannot_write_and_revocation_denies_replay(self):
        with self.assertRaises(CommandDenied):
            self.change(actor=self.other, target=self.admin)
        denied = AuditEvent.objects.get(outcome="DENIED")
        self.assertEqual(denied.actor_user_id, self.other.pk)
        self.assertEqual(denied.roles_at_time, ["CREW"])
        self.change()
        op = str(uuid4())
        self.change(actor=self.other, target=self.admin, role="SUPERVISOR", operation=op)
        self.change(action="REVOKE")
        with self.assertRaises(CommandDenied):
            self.change(actor=self.other, target=self.admin, role="SUPERVISOR", operation=op)

    def test_another_actor_cannot_replay_receipt(self):
        self.change()
        op = str(uuid4())
        self.change(role="SUPERVISOR", operation=op)
        with self.assertRaises(OperationConflict):
            self.change(actor=self.other, target=self.admin, role="SUPERVISOR", operation=op)

    def test_last_admin_revoke_and_disable_refused_and_failure_audited(self):
        with self.assertRaises(LastAdministrator):
            self.change(target=self.admin, action="REVOKE")
        with self.assertRaises(LastAdministrator):
            set_user_active(active=False, **self.context(target=self.admin))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.active)
        self.assertEqual(AuditEvent.objects.filter(outcome="FAILED").count(), 2)
        self.assertIn("ADMINISTRATOR", current_role_codes(self.admin))

    def test_disable_reenable_and_stale_request_identity(self):
        self.change()
        set_user_active(active=False, **self.context())
        self.assertEqual(current_role_codes(self.other), ())
        with self.assertRaises(CommandDenied):
            self.change(actor=self.other, target=self.admin)
        set_user_active(active=True, **self.context())
        self.assertIn("ADMINISTRATOR", current_role_codes(self.other))

    def test_audit_failure_rolls_back_role_receipt_then_records_failure(self):
        count = (RoleGrant.objects.count(), CommandReceipt.objects.count())
        from cya.core.audit_repository import append_audit

        def fail_success(**kwargs):
            if kwargs["outcome"] == "SUCCEEDED":
                raise RuntimeError("synthetic success audit failure")
            return append_audit(**kwargs)

        with patch("cya.core.audit_commands.append_audit", side_effect=fail_success):
            with self.assertRaises(RuntimeError):
                self.change(role="SUPERVISOR")
        self.assertEqual(count, (RoleGrant.objects.count(), CommandReceipt.objects.count()))
        self.assertEqual(current_role_codes(self.other), ("CREW",))
        self.assertTrue(AuditEvent.objects.filter(outcome="FAILED").exists())

    def test_request_uses_persisted_current_roles_and_audits_unknown_action(self):
        request = RequestFactory().post("/synthetic/protected/", {"roles": "ADMINISTRATOR"})
        request.cya_user = self.other
        calls = []

        @permission_required("manage_users_roles", organization_loader=lambda request: self.org.pk)
        def view(request):
            calls.append(True)
            return HttpResponse("ok")

        self.assertEqual(view(request).status_code, 403)
        self.change()
        self.assertEqual(view(request).status_code, 200)
        self.change(action="REVOKE")
        self.assertEqual(view(request).status_code, 403)
        self.assertEqual(calls, [True])

        @permission_required(
            "unlisted arbitrary input", organization_loader=lambda request: self.org.pk
        )
        def unknown(request):
            self.fail("Unknown action ran")

        self.assertEqual(unknown(request).status_code, 403)
        self.assertTrue(
            AuditEvent.objects.filter(action="UNLISTED_ACTION", outcome="DENIED").exists()
        )

    def test_guarded_body_rolls_back_before_durable_denial(self):
        request = RequestFactory().post("/synthetic/protected/")
        request.cya_user = self.other

        @permission_required("record_check", organization_loader=lambda request: self.org.pk)
        def view(request):
            Organization.objects.filter(pk=self.org.pk).update(name="Transient")
            require_permission(None, self.org.pk, "manage_users_roles")

        self.assertEqual(view(request).status_code, 403)
        self.org.refresh_from_db()
        self.assertEqual(self.org.name, "Sample Agency")
        self.assertTrue(AuditEvent.objects.filter(outcome="DENIED").exists())

    def test_operator_refusal_and_ambiguous_writer_are_audited_without_retry(self):
        with self.assertRaises(CommandError):
            call_command(
                "bootstrap_administrator",
                organization=self.org.pk,
                user=self.other.pk,
                stdout=io.StringIO(),
            )
        self.assertTrue(
            AuditEvent.objects.filter(action="BOOTSTRAP_ADMINISTRATOR", outcome="DENIED").exists()
        )
        self.org = Organization.objects.create(name="Fresh Sample Agency", station_zone="UTC")
        target = self.member("fresh")
        with patch.object(
            DatabaseRoleBackend,
            "append_role_change",
            side_effect=OperationalError(2013, "synthetic lost response"),
        ) as writer:
            with self.assertRaises(CommandError):
                call_command(
                    "bootstrap_administrator",
                    organization=self.org.pk,
                    user=target.pk,
                    stdout=io.StringIO(),
                )
        writer.assert_called_once()
        event = AuditEvent.objects.get(organization=self.org, outcome="OUTCOME_UNKNOWN")
        self.assertEqual(event.service_identity, "operator.bootstrap")
        self.assertEqual(current_role_codes(target), ("CREW",))

    def test_real_shell_denial_is_durable_and_audit_outage_is_503(self):
        from cya.shell.views import administration

        request = RequestFactory().get("/administration/")
        request.cya_user = self.other
        self.assertEqual(administration(request).status_code, 403)
        self.assertTrue(
            AuditEvent.objects.filter(action="VIEW_ADMINISTRATION", outcome="DENIED").exists()
        )
        with patch(
            "cya.authorization.persistence.append_audit",
            side_effect=RuntimeError("synthetic unavailable"),
        ):
            self.assertEqual(administration(request).status_code, 503)

    def test_concurrent_last_two_admin_mutations_preserve_one(self):
        self.change()
        barrier = Barrier(2)

        def remove(actor_id, active):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                kwargs = dict(
                    organization_id=self.org.pk,
                    actor_id=actor_id,
                    target_id=actor_id,
                    operation_id=str(uuid4()),
                    correlation_id=str(uuid4()),
                    request_attempt_id=str(uuid4()),
                )
                try:
                    if active:
                        set_user_active(active=False, **kwargs)
                    else:
                        change_role(role="ADMINISTRATOR", action="REVOKE", **kwargs)
                    return "saved"
                except LastAdministrator:
                    return "protected"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(remove, self.admin.pk, False)
            b = pool.submit(remove, self.other.pk, True)
            self.assertCountEqual(
                [a.result(timeout=20), b.result(timeout=20)], ["saved", "protected"]
            )
        active_admins = [
            u
            for u in User.objects.filter(organization=self.org, active=True)
            if "ADMINISTRATOR" in current_role_codes(u)
        ]
        self.assertEqual(len(active_admins), 1)

    def test_role_database_guards_reject_mutation_and_bad_evidence(self):
        grant = RoleGrant.objects.filter(user=self.other).get()
        for operation in (
            lambda: RoleGrant.objects.filter(pk=grant.pk).update(role="ADMINISTRATOR"),
            lambda: RoleGrant.objects.filter(pk=grant.pk).delete(),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                operation()
        fields = dict(
            organization=self.org,
            user=self.other,
            command=grant.command,
            actor_user=None,
            actor_roles_at_time=[],
            recorded_at=grant.recorded_at,
            sequence=2,
            role="CREW",
            action="GRANT",
        )
        for changes in (
            {},
            {"role": "Supervisor"},
            {"role": "SUPERVISOR", "sequence": 4},
            {"role": "SUPERVISOR", "actor_user": self.admin},
            {
                "role": "SUPERVISOR",
                "organization": Organization.objects.create(
                    name="Other Sample Agency", station_zone="UTC"
                ),
            },
            {"action": "REVOKE", "prior_grant": RoleGrant.objects.filter(user=self.admin).first()},
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                RoleGrant.objects.create(**(fields | changes))

    def test_role_command_type_guard(self):
        from cya.core.audit_repository import append_receipt

        grant = RoleGrant.objects.filter(user=self.other).get()
        # 1. Negative test: receipt of another command type is refused
        with transaction.atomic():
            wrong_receipt = append_receipt(
                organization=self.org,
                actor_user=self.admin,
                operation_id=str(uuid4()),
                command_type="CHECKLIST_PUBLISH",
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            RoleGrant.objects.create(
                organization=self.org,
                user=self.other,
                command=wrong_receipt,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
                recorded_at=wrong_receipt.recorded_at,
                sequence=grant.sequence + 1,
                role="SUPERVISOR",
                action="GRANT",
            )
        # 2. Positive tests: receipts with allowed command types are accepted
        for cmd_type in ("ROLE_CHANGE", "INITIAL_CREW", "BOOTSTRAP_ADMINISTRATOR", "INITIAL_EMT"):
            with transaction.atomic():
                rcpt = append_receipt(
                    organization=self.org,
                    actor_user=self.admin,
                    operation_id=str(uuid4()),
                    command_type=cmd_type,
                    request_digest="0" * 64,
                    response_status=200,
                    response_body={},
                    correlation_id=str(uuid4()),
                )
                # Create a fresh user so sequence and grant state are clean
                u = User.objects.create(
                    organization=self.org,
                    idp_issuer="https://identity.example.test",
                    idp_subject=f"user-{cmd_type.lower()}",
                    email=f"{cmd_type.lower()}@example.test",
                )
                row = RoleGrant.objects.create(
                    organization=self.org,
                    user=u,
                    command=rcpt,
                    actor_user=self.admin,
                    actor_roles_at_time=["ADMINISTRATOR"],
                    recorded_at=rcpt.recorded_at,
                    sequence=1,
                    role="CREW",
                    action="GRANT",
                )
            self.assertIsNotNone(row.pk)
