"""Identity tables on real MariaDB: immutability triggers and the setup command."""

import io
import uuid

from django.core.management import CommandError, call_command
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from cya.accounts.models import Organization, User


class IdentityImmutabilityTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.organization,
            idp_issuer="https://accounts.google.com",
            idp_subject="1001",
            email="1001@agency.example",
        )

    def assert_update_refused(self, queryset, **changes):
        with self.assertRaises(DatabaseError), transaction.atomic():
            queryset.update(**changes)

    def test_user_identity_columns_cannot_change(self):
        users = User.objects.filter(pk=self.user.pk)
        other = Organization.objects.create(name="Other", station_zone="UTC")
        for changes in (
            {"idp_subject": "2002"},
            {"idp_issuer": "accounts.google.com"},
            {"organization": other},
            {"id": str(uuid.uuid4())},
            {"idp_subject": "1001 "},
        ):
            with self.subTest(changes=list(changes)):
                self.assert_update_refused(users, **changes)
        self.user.refresh_from_db()
        self.assertEqual(self.user.idp_subject, "1001")

    def test_user_attributes_and_active_flag_can_change(self):
        User.objects.filter(pk=self.user.pk).update(
            email="renamed@agency.example", display_name="Renamed", active=False
        )
        self.user.refresh_from_db()
        self.assertEqual((self.user.email, self.user.active), ("renamed@agency.example", False))

    def test_organization_identity_cannot_change_but_name_can(self):
        organizations = Organization.objects.filter(pk=self.organization.pk)
        self.assert_update_refused(organizations, id=str(uuid.uuid4()))
        organizations.update(name="Renamed Agency")

    def test_users_cannot_be_deleted(self):
        # Attribution is kept: deactivate instead (AUTH-2).
        with self.assertRaises(DatabaseError), transaction.atomic():
            User.objects.filter(pk=self.user.pk).delete()
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_subjects_are_case_sensitive_identities(self):
        # A provider subject differing only in case is a different identity,
        # and a case-only change to an existing subject is refused.
        User.objects.create(
            organization=self.organization,
            idp_issuer="https://accounts.google.com",
            idp_subject="abc",
            email="a@agency.example",
        )
        User.objects.create(
            organization=self.organization,
            idp_issuer="https://accounts.google.com",
            idp_subject="ABC",
            email="b@agency.example",
        )
        self.assert_update_refused(User.objects.filter(idp_subject="abc"), idp_subject="aBc")

    def test_non_canonical_ids_are_refused(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Organization.objects.create(id="NOT-A-UUID", name="Bad", station_zone="UTC")

    def test_identity_tuple_is_unique_and_nonempty(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                organization=self.organization,
                idp_issuer="https://accounts.google.com",
                idp_subject="1001",
                email="someone-else@agency.example",
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(
                organization=self.organization, idp_issuer=" ", idp_subject="x", email="e"
            )


class CreateOrganizationCommandTests(TestCase):
    def run_command(self, *args):
        out = io.StringIO()
        call_command("create_organization", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_writes_nothing_then_create_then_idempotent(self):
        args = ["--name", "Sample Agency", "--time-zone", "America/New_York"]
        self.assertIn("Dry run", self.run_command(*args, "--dry-run"))
        self.assertFalse(Organization.objects.exists())
        self.assertIn("Created", self.run_command(*args))
        self.assertIn("Unchanged", self.run_command("--name", "Another", "--time-zone", "UTC"))
        self.assertEqual(Organization.objects.get().name, "Sample Agency")

    def test_invalid_arguments_are_refused(self):
        for args in (
            ["--name", " ", "--time-zone", "UTC"],
            ["--name", "X", "--time-zone", "Mars/Base"],
        ):
            with self.subTest(args=args), self.assertRaises(CommandError):
                self.run_command(*args)
        self.assertFalse(Organization.objects.exists())
