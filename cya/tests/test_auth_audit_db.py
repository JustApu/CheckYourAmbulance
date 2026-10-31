from datetime import UTC, datetime
from uuid import uuid4

from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase, override_settings

from cya.accounts.models import Organization, User
from cya.accounts.signin import AuthAuditEvent
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.core.auth_audit import record_auth_event
from cya.core.models import AuditEvent


def current_roles(user):
    return ("EMT",)


@override_settings(CYA_ROLE_LOOKUP="cya.tests.test_auth_audit_db.current_roles")
class AuthenticationAuditTests(TransactionTestCase):
    def setUp(self):
        self.addCleanup(call_command, "flush", verbosity=0, interactive=False, reset_sequences=True)
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.org,
            idp_issuer="https://idp.example.test",
            idp_subject="member",
            email="member@example.test",
        )

    def event(self, **changes):
        kwargs = dict(
            action="SIGN_IN",
            outcome="SUCCEEDED",
            reason_code="",
            organization_id=str(self.org.pk),
            user_id=str(self.user.pk),
            correlation_id=str(uuid4()),
            recorded_at=datetime.now(UTC),
        )
        kwargs.update(changes)
        return AuthAuditEvent(**kwargs)

    def test_profile_values_and_role_snapshot_persist_inside_business_transaction(self):
        with transaction.atomic():
            User.objects.filter(pk=self.user.pk).update(display_name="Updated Synthetic")
            record_auth_event(
                self.event(
                    action="USER_PROFILE_UPDATED",
                    changes=(("display_name", "", "Updated Synthetic"),),
                )
            )
        audit = AuditEvent.objects.get()
        self.assertEqual(audit.roles_at_time, ["EMT"])
        self.assertEqual(audit.prior_snapshot["values"], {"display_name": ""})
        self.assertEqual(audit.new_snapshot["values"], {"display_name": "Updated Synthetic"})
        self.assertEqual(audit.target_id, str(self.user.pk))

    def test_rolled_back_success_does_not_survive_but_refusal_commits_afterwards(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                record_auth_event(self.event())
                User.objects.filter(pk=self.user.pk).update(display_name="Must roll back")
                raise ValueError("synthetic")
        self.assertEqual(AuditEvent.objects.count(), 0)
        record_auth_event(
            self.event(
                action="SIGN_IN",
                outcome="REFUSED",
                reason_code="user_inactive",
                organization_id=None,
                user_id=None,
            )
        )
        audit = AuditEvent.objects.get()
        self.assertEqual((audit.outcome, audit.reason_code), ("DENIED", "USER_INACTIVE"))
        self.assertIsNone(audit.actor_user_id)
        self.assertEqual(audit.roles_at_time, [])
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "")

    @override_settings(CYA_ROLE_LOOKUP=None)
    def test_missing_role_reader_and_invalid_snapshots_fail_closed(self):
        with self.assertLogs("cya.core.auth_audit", level="ERROR"):
            with self.assertRaises(AuditPersistenceFailure):
                record_auth_event(self.event())
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_raw_or_unknown_profile_fields_are_never_persisted(self):
        with self.assertLogs("cya.core.auth_audit", level="ERROR"):
            with self.assertRaises(AuditPersistenceFailure):
                record_auth_event(
                    self.event(
                        action="USER_PROFILE_UPDATED", changes=(("access_token", "", "synthetic"),)
                    )
                )
        self.assertEqual(AuditEvent.objects.count(), 0)
