"""Actual runtime grants for the status tables against disposable MariaDB."""

import io
import os
from unittest import skipUnless
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase

from cya.authorization.roles import grant_initial_crew
from cya.checklists.models import Vehicle
from cya.operations import commands
from cya.operations.models import StatusRequest, VehicleStatusEvent
from cya.tests import test_audit_runtime_db as runtime_tests


def ids():
    return {
        "operation_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "request_attempt_id": str(uuid4()),
    }


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class StatusRuntimeTests(TransactionTestCase):
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
        self.vehicle = Vehicle.objects.create(organization=self.org, display_name="Synthetic")
        # The supported installer must grant the append-only status tables.
        call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())

    def test_runtime_commands_and_forbidden_update_delete_ddl(self):
        context = {"organization_id": str(self.org.pk), "actor_id": str(self.user.pk)}
        with self.runtime() as wrapper:
            commands.set_vehicle_status(
                vehicle_id=str(self.vehicle.pk),
                status="OUT_OF_SERVICE",
                explanation="Synthetic.",
                **context,
                **ids(),
            )
            asked = commands.request_status_change(
                vehicle_id=str(self.vehicle.pk),
                requested_status="IN_SERVICE",
                reason="Synthetic.",
                **context,
                **ids(),
            )
            commands.decide_status_request(
                request_id=asked.body["request_id"],
                decision="APPROVE",
                explanation="Synthetic.",
                **context,
                **ids(),
            )
            self.assertEqual(VehicleStatusEvent.objects.count(), 2)
            self.assertEqual(StatusRequest.objects.count(), 2)
            for table in ("vehicle_status_event", "status_request"):
                for sql in (
                    f"UPDATE {table} SET recorded_at=recorded_at",
                    f"DELETE FROM {table}",
                    f"TRUNCATE TABLE {table}",
                    f"ALTER TABLE {table} ADD COLUMN unsafe INT",
                ):
                    with (
                        self.subTest(sql=sql),
                        self.assertRaises(DatabaseError) as caught,
                        wrapper.cursor() as cursor,
                    ):
                        cursor.execute(sql)
                    self.assertEqual(caught.exception.args[0], 1142)
