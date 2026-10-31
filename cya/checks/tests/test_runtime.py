"""Actual runtime grants for check_record against disposable MariaDB."""

import io
import os
from datetime import timedelta
from unittest import skipUnless
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cya.authorization.roles import grant_initial_crew
from cya.checklists import commands as checklist_commands
from cya.checklists.models import ChecklistTemplate, Compartment, Item, Vehicle
from cya.checks.commands import correct_check, record_check
from cya.checks.models import CheckEvent, CheckRecord
from cya.deficiencies.models import Deficiency, DeficiencyCategory
from cya.deficiencies.severity import ensure_default_severities
from cya.tests import test_audit_runtime_db as runtime_tests


def ids():
    return {
        "operation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "request_attempt_id": str(uuid4()),
    }


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class CheckRuntimeTests(TransactionTestCase):
    runtime = runtime_tests.RuntimePrivilegeTests.runtime
    drop_account = runtime_tests.RuntimePrivilegeTests.drop_account

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def setUp(self):
        runtime_tests.RuntimePrivilegeTests.setUp(self)
        with transaction.atomic():
            grant_initial_crew(user=self.user, command_id=str(uuid4()))
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.user.pk,
            stdout=io.StringIO(),
        )
        self.context = {"organization_id": str(self.org.pk), "actor_id": str(self.user.pk)}
        vehicle = Vehicle.objects.create(organization=self.org, display_name="Synthetic")
        cabinet = Compartment.objects.create(
            organization=self.org,
            vehicle=vehicle,
            stable_slot_id=str(uuid4()),
            revision=1,
            label="Synthetic cabinet",
            effective_at=timezone.now() - timedelta(days=1),
        )
        item = Item.objects.create(organization=self.org, label="Synthetic suction")
        template = ChecklistTemplate.objects.create(organization=self.org, label="Synthetic")
        version_id = checklist_commands.save_template_draft(
            template_id=str(template.pk),
            items=[
                {
                    "item_id": str(item.pk),
                    "display_order": 10,
                    "kind": "PRESENCE_CONDITION",
                    "instructions": "Synthetic.",
                    "allowed_outcomes": ["PASSED", "FAILED", "UNABLE_TO_INSPECT"],
                    "compartment_id": str(cabinet.pk),
                }
            ],
            **self.context,
            **ids(),
        ).body["version_id"]
        checklist_commands.publish_template_version(version_id=version_id, **self.context, **ids())
        self.fields = {
            "vehicle_id": str(vehicle.pk),
            "item_id": str(item.pk),
            "checklist_version_id": version_id,
            "outcome": "PASSED",
            "purpose": "ROUTINE",
        }
        # The supported installer must grant the append-only check table.
        call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())

    def test_runtime_records_a_check_and_cannot_update_delete_or_alter_it(self):
        with self.runtime() as wrapper:
            result = record_check(**self.fields, **self.context, **ids())
            self.assertEqual(result.status, 201)
            self.assertTrue(CheckRecord.objects.filter(pk=result.body["check_id"]).exists())
            corrected = correct_check(
                check_id=result.body["check_id"],
                expected_tip_check_id=result.body["check_id"],
                outcome="PASSED",
                **self.context,
                **ids(),
            )
            self.assertTrue(CheckRecord.objects.filter(pk=corrected.body["check_id"]).exists())
            self.assertTrue(CheckEvent.objects.filter(pk=corrected.body["event_id"]).exists())
            for sql in (
                "UPDATE check_record SET recorded_at=recorded_at",
                "DELETE FROM check_record",
                "TRUNCATE TABLE check_record",
                "ALTER TABLE check_record ADD COLUMN unsafe INT",
                "UPDATE check_event SET recorded_at=recorded_at",
                "DELETE FROM check_event",
                "TRUNCATE TABLE check_event",
                "ALTER TABLE check_event ADD COLUMN unsafe INT",
            ):
                with (
                    self.subTest(sql=sql),
                    self.assertRaises(DatabaseError) as caught,
                    wrapper.cursor() as cursor,
                ):
                    cursor.execute(sql)
                self.assertEqual(caught.exception.args[0], 1142)

    def test_runtime_creates_a_deficiency_and_cannot_rewrite_its_history(self):
        category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=str(uuid4()),
            revision=1,
            label="Synthetic",
            supply_related=False,
            effective_at=timezone.now() - timedelta(days=1),
        )
        ensure_default_severities(self.org.pk, timezone.now() - timedelta(days=1))
        fields = {
            **self.fields,
            "outcome": "FAILED",
            "deficiency": {
                "create": {
                    "category_id": category.stable_category_id,
                    "severity": "LOW",
                    "summary": "Synthetic",
                    "description": "Synthetic.",
                }
            },
        }
        with self.runtime() as wrapper:
            result = record_check(**fields, **self.context, **ids())
            deficiency_id = result.body["deficiency_ids"][0]
            self.assertTrue(Deficiency.objects.filter(pk=deficiency_id).exists())
            # Current state is the projection's alone; the runtime may update only that.
            with wrapper.cursor() as cursor:
                cursor.execute(
                    "UPDATE deficiency_projection SET status='IN_PROGRESS' WHERE deficiency_id=%s",
                    [deficiency_id],
                )
            for sql in (
                "UPDATE deficiency SET summary='changed'",
                "DELETE FROM deficiency",
                "UPDATE deficiency_event SET recorded_at=recorded_at",
                "DELETE FROM deficiency_event",
                "UPDATE deficiency_evidence SET note='x'",
                "DELETE FROM deficiency_evidence",
                "UPDATE deficiency_affected_item SET recorded_at=recorded_at",
                "DELETE FROM deficiency_affected_item",
                "UPDATE deficiency_link SET recorded_at=recorded_at",
                "DELETE FROM deficiency_link",
                "UPDATE deficiency_category SET supply_related=1",
                "DELETE FROM deficiency_category",
                "UPDATE deficiency_severity SET active=0",
                "DELETE FROM deficiency_severity",
                "DELETE FROM deficiency_projection",
                "DELETE FROM deficiency_lock",
                "TRUNCATE TABLE deficiency_event",
                "ALTER TABLE deficiency ADD COLUMN unsafe INT",
            ):
                with (
                    self.subTest(sql=sql),
                    self.assertRaises(DatabaseError) as caught,
                    wrapper.cursor() as cursor,
                ):
                    cursor.execute(sql)
                self.assertEqual(caught.exception.args[0], 1142)
