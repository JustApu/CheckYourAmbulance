"""Actual runtime grants for kit_assignment_event against disposable MariaDB."""

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
from cya.checklists.models import Compartment, KitInstance, Vehicle
from cya.kits.commands import add_kit_instance, record_kit_assignment
from cya.kits.models import KitAssignmentEvent
from cya.tests import test_audit_runtime_db as runtime_tests


def ids():
    return {
        "operation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "request_attempt_id": str(uuid4()),
    }


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class KitRuntimeTests(TransactionTestCase):
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
        slot = str(uuid4())
        self.vehicles = [
            Vehicle.objects.create(organization=self.org, display_name=f"Synthetic {n}")
            for n in ("A", "B")
        ]
        self.cabinets = [
            Compartment.objects.create(
                organization=self.org,
                vehicle=v,
                stable_slot_id=slot,
                revision=1,
                label="Synthetic cabinet",
                effective_at=timezone.now() - timedelta(days=1),
            )
            for v in self.vehicles
        ]
        call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())

    def test_runtime_adds_and_moves_a_kit_but_cannot_rewrite_its_history(self):
        with self.runtime() as wrapper:
            added = add_kit_instance(
                label="Synthetic kit",
                vehicle_id=str(self.vehicles[0].pk),
                compartment_id=str(self.cabinets[0].pk),
                **self.context,
                **ids(),
            )
            record_kit_assignment(
                kit_id=added.body["kit_id"],
                active=True,
                vehicle_id=str(self.vehicles[1].pk),
                compartment_id=str(self.cabinets[1].pk),
                **self.context,
                **ids(),
            )
            self.assertEqual(KitAssignmentEvent.objects.count(), 2)
            self.assertEqual(KitInstance.objects.get().vehicle_id, self.vehicles[1].pk)
            for sql in (
                "UPDATE kit_assignment_event SET active=active",
                "DELETE FROM kit_assignment_event",
                "TRUNCATE TABLE kit_assignment_event",
                "ALTER TABLE kit_assignment_event ADD COLUMN unsafe INT",
                "DELETE FROM kit_instance",
            ):
                with (
                    self.subTest(sql=sql),
                    self.assertRaises(DatabaseError) as caught,
                    wrapper.cursor() as cursor,
                ):
                    cursor.execute(sql)
                self.assertEqual(caught.exception.args[0], 1142)
