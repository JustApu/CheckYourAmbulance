from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from cya.accounts.models import Organization, User
from cya.authorization.locking import locked_identities


class IdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Example Agency", station_zone="UTC")
        cls.user = User.objects.create(
            organization=cls.organization,
            idp_issuer="https://identity.example.test",
            idp_subject="stable-subject",
            email="member@example.test",
            display_name="Example Member",
        )

    def assert_rejected(self, sql, values):
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(sql, values)

    def test_identity_uses_subject_and_issuer_instead_of_email(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            User.objects.create(
                organization=self.organization,
                idp_issuer=self.user.idp_issuer,
                idp_subject=self.user.idp_subject,
                email="changed@example.test",
                display_name="Another Member",
            )
        other = User.objects.create(
            organization=self.organization,
            idp_issuer=self.user.idp_issuer,
            idp_subject="another-subject",
            email=self.user.email,
            display_name="Another Member",
        )
        self.assertNotEqual(other.pk, self.user.pk)

    def test_provider_identity_is_case_sensitive(self):
        other = User.objects.create(
            organization=self.organization,
            idp_issuer=self.user.idp_issuer,
            idp_subject="Stable-subject",
            email="another@example.test",
            display_name="Another Member",
        )
        self.assertNotEqual(other.pk, self.user.pk)

    def test_identity_anchors_cannot_be_reassigned_or_deleted(self):
        organization = Organization.objects.create(name="Another Agency", station_zone="UTC")
        for field, value in (
            ("id", str(uuid4())),
            ("organization_id", organization.pk),
            ("idp_issuer", "https://other.example.test"),
            ("idp_subject", "replacement-subject"),
        ):
            with self.subTest(field=field):
                # SQL column names are fixed test constants, never request data.
                self.assert_rejected(
                    f"UPDATE `user` SET `{field}` = %s WHERE id = %s", [value, self.user.pk]
                )
        self.assert_rejected("DELETE FROM `user` WHERE id = %s", [self.user.pk])
        self.user.refresh_from_db()
        self.assertEqual(self.user.idp_subject, "stable-subject")

    def test_mutable_profile_and_inactive_attribution_are_retained(self):
        User.objects.filter(pk=self.user.pk).update(
            email="updated@example.test", display_name="Updated Member", active=False
        )
        self.user.refresh_from_db()
        self.assertFalse(self.user.active)
        self.assertEqual(self.user.email, "updated@example.test")
        self.assertEqual(self.user.idp_subject, "stable-subject")

    def test_database_rejects_missing_organization_invalid_booleans_and_ids(self):
        for field, value in (("organization_id", str(uuid4())), ("active", 2)):
            with self.subTest(field=field):
                self.assert_rejected(
                    f"UPDATE `user` SET `{field}` = %s WHERE id = %s", [value, self.user.pk]
                )
        self.assert_rejected(
            "INSERT INTO organization (id,name,station_zone,active) VALUES (%s,%s,%s,%s)",
            ["not-a-uuid", "Example", "UTC", 1],
        )
        # A new row exercises the FK independently of the immutable-user trigger.
        with self.assertRaises(DatabaseError), transaction.atomic():
            User.objects.create(
                organization_id=str(uuid4()),
                idp_issuer="https://identity.example.test",
                idp_subject="missing-organization",
                email="missing@example.test",
                display_name="Missing Organization",
            )
        field = User._meta.get_field("id")
        for bad in ("not-a-uuid", "ABCDEFAB-1234-4567-89AB-ABCDEFABCDEF"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                field.clean(bad, self.user)

    def test_physical_uuid_contract(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, CHARACTER_SET_NAME, COLLATION_NAME "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'user' AND COLUMN_NAME IN ('id','organization_id') "
                "ORDER BY COLUMN_NAME"
            )
            self.assertEqual(cursor.fetchall(), (("char", 36, "ascii", "ascii_bin"),) * 2)

    def test_lock_helper_rejects_missing_and_cross_organization_users(self):
        other = Organization.objects.create(name="Another Agency", station_zone="UTC")
        with self.assertRaises(User.DoesNotExist):
            with locked_identities(other.pk, [self.user.pk]):
                self.fail("Cross-organization identity must not be yielded")
        with self.assertRaises(User.DoesNotExist):
            with locked_identities(self.organization.pk, [str(uuid4())]):
                self.fail("Missing identity must not be yielded")

    def test_identity_lock_context_rolls_back_failed_mutations(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with locked_identities(self.organization.pk, [self.user.pk]) as (_, users):
                user = users[self.user.pk]
                user.active = False
                user.save(update_fields=["active"])
                raise RuntimeError("synthetic failure")
        self.user.refresh_from_db()
        self.assertTrue(self.user.active)
