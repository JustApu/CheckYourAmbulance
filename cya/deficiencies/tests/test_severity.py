"""Severity levels are administrator data with criteria shown to the reporter (DEF-8, OQ-56)."""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cya.accounts.models import Organization
from cya.checks.commands import CheckError
from cya.deficiencies.commands import report_deficiency
from cya.deficiencies.models import Deficiency, DeficiencySeverity
from cya.deficiencies.service import DeficiencyError
from cya.deficiencies.severity import (
    DEFAULT_LEVELS,
    active_levels,
    ensure_default_severities,
    resolve_severity,
)
from cya.deficiencies.tests.support import DeficiencyCase


class SeverityLevelTests(DeficiencyCase):
    def report(self, **extra):
        fields = {"vehicle_id": str(self.vehicle.pk), "target": {"item_id": str(self.suction.pk)}}
        return report_deficiency(**fields, **self.new_fields() | extra, **self.context(self.crew))

    def rejected(self, code, **kwargs):
        before = self.counts()
        with self.assertRaises(DeficiencyError) as caught:
            self.report(**kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.counts(), before)

    def add_revision(self, code, revision, **fields):
        values = {
            "label": code.title(),
            "description": f"Synthetic criteria for {code}.",
            "rank": 1,
            "active": True,
            "effective_at": self.now - timedelta(minutes=5),
            **fields,
        }
        return DeficiencySeverity.objects.create(
            organization=self.org, code=code, revision=revision, **values
        )

    def test_the_defaults_have_ordered_levels_each_with_concise_criteria(self):
        levels = active_levels(str(self.org.pk), timezone.now())
        self.assertEqual([lv.code for lv in levels], ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
        self.assertEqual([lv.rank for lv in levels], [1, 2, 3, 4])
        for level in levels:
            self.assertTrue(level.label and level.description)
            self.assertLessEqual(len(level.description), 160, level.code)
        self.assertEqual(len(DEFAULT_LEVELS), 4)

    def test_seeding_is_idempotent_and_never_overwrites_an_edited_level(self):
        before = DeficiencySeverity.objects.count()
        self.assertEqual(ensure_default_severities(self.org.pk, timezone.now()), 0)
        self.assertEqual(DeficiencySeverity.objects.count(), before)

    def test_the_test_organization_starts_with_the_default_levels(self):
        self.assertEqual(
            DeficiencySeverity.objects.filter(organization=self.org).count(), len(DEFAULT_LEVELS)
        )

    def test_a_new_level_can_be_added_and_used(self):
        self.add_revision("URGENT", 1, rank=5)
        result = self.report(severity="URGENT")
        self.assertEqual(
            Deficiency.objects.get(pk=result.body["deficiency_id"]).severity_version.code, "URGENT"
        )
        self.assertEqual(
            [lv.code for lv in active_levels(str(self.org.pk), timezone.now())][-1], "URGENT"
        )

    def test_a_reworded_level_applies_to_new_reports_and_old_ones_keep_their_revision(self):
        old = self.report(severity="HIGH").body["deficiency_id"]
        new_revision = self.add_revision("HIGH", 2, label="Serious", rank=3)
        fresh = self.report(severity="HIGH").body["deficiency_id"]
        self.assertEqual(Deficiency.objects.get(pk=old).severity_version_id, self.level("HIGH").pk)
        self.assertEqual(Deficiency.objects.get(pk=fresh).severity_version_id, new_revision.pk)

    def test_a_retired_or_unknown_or_future_level_is_refused(self):
        self.add_revision("LOW", 2, active=False)
        self.add_revision("MEDIUM", 2, effective_at=timezone.now() + timedelta(days=1))
        self.rejected("INVALID_SEVERITY", severity="LOW")
        self.rejected("INVALID_SEVERITY", severity="NOPE")
        self.rejected("INVALID_SEVERITY", severity="low")
        self.report(severity="MEDIUM")  # the future revision is not yet in force; revision 1 is
        self.assertIsNone(resolve_severity(str(self.org.pk), "LOW", timezone.now()))

    def test_a_levels_code_must_be_text_of_bounded_length(self):
        for value in (5, "", None, "X" * 31, ["HIGH"]):
            with self.subTest(value=value), self.assertRaises(DeficiencyError) as caught:
                self.report(severity=value)
            self.assertEqual(caught.exception.code, "MALFORMED_REQUEST")

    def test_another_organizations_levels_are_not_visible(self):
        other = Organization.objects.create(name="Synthetic other", station_zone="UTC")
        ensure_default_severities(other.pk, timezone.now() - timedelta(days=1))
        self.assertEqual(len(active_levels(str(other.pk), timezone.now())), 4)
        self.assertEqual(len(active_levels(str(self.org.pk), timezone.now())), 4)

    def test_a_published_level_is_frozen_in_the_database(self):
        rows = DeficiencySeverity.objects.filter(pk=self.level("HIGH").pk)
        for operation in (lambda: rows.update(description="Changed."), rows.delete):
            with self.assertRaises(DatabaseError), transaction.atomic():
                operation()
        self.assertNotEqual(self.level("HIGH").description, "Changed.")

    def test_checks_resolve_the_level_too(self):
        self.add_revision("LOW", 2, active=False)
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.failed(create=self.new_fields(severity="LOW"))
        self.assertEqual(
            (caught.exception.code, caught.exception.path),
            ("INVALID_SEVERITY", "deficiency.create.severity"),
        )
        self.assertEqual(self.counts(), before)
        self.failed(create=self.new_fields(severity="HIGH"))


class CreateOrganizationSeedTests(TransactionTestCase):
    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)

    def test_create_organization_starts_with_the_default_levels_once(self):
        call_command(
            "create_organization", name="Sample Agency", time_zone="UTC", stdout=StringIO()
        )
        organization = Organization.objects.get()
        codes = list(
            DeficiencySeverity.objects.filter(organization=organization)
            .order_by("rank")
            .values_list("code", flat=True)
        )
        self.assertEqual(codes, ["LOW", "MEDIUM", "HIGH", "CRITICAL"])
        call_command(
            "create_organization", name="Sample Agency", time_zone="UTC", stdout=StringIO()
        )
        self.assertEqual(DeficiencySeverity.objects.count(), 4)
