"""The actual runtime account: it can generate and reconcile, and can mutate nothing afterwards.

The runtime account holds SELECT and INSERT on the append-only obligation tables and no UPDATE,
DELETE or DDL (contracts section 3). These tests run generation, publication reconciliation and
binding through that real account against the disposable MariaDB, then try every forbidden
statement. They need the disposable DBA principal CI provides (CYA_DB_SECURITY_TESTS=1).
"""

import io
import os
import secrets
from unittest import skipUnless
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, connections

from cya.obligations.models import Obligation, ObligationSelection
from cya.obligations.tests.support import ObligationCase
from cya.tests import test_audit_runtime_db as runtime_tests

TABLES = (
    "requirement_binding",
    "requirement_version",
    "obligation",
    "obligation_selection",
    "odometer_obligation",
)


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class ObligationRuntimeTests(ObligationCase):
    runtime = runtime_tests.RuntimePrivilegeTests.runtime

    def setUp(self):
        super().setUp()
        self.owner = connections["default"]
        self.username = "cya_test_" + uuid4().hex[:16]
        self.password = secrets.token_hex(24)
        self.account = f"'{self.username}'@'%'"
        with self.owner.cursor() as cursor:
            cursor.execute(
                "CREATE USER %s@%s IDENTIFIED BY %s", [self.username, "%", self.password]
            )
        self.addCleanup(self.drop_account)
        # The supported installer, not hand-written grants; replaying it must be safe.
        for _ in range(2):
            call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())
        self.v1 = self.published_version()
        self.publish_schedule("test-0600")

    def drop_account(self):
        with self.owner.cursor() as cursor:
            cursor.execute(f"DROP USER IF EXISTS {self.account}")

    def test_runtime_account_generates_reconciles_and_binds_with_insert_only_privileges(self):
        with self.runtime() as wrapper:
            self.bind(reconcile=False)
            summary = self.generate()
            self.assertEqual(summary.failures, [])
            self.assertEqual(len(self.obligations()), 6)
            self.clock.set(self.at(hours=4))
            v2 = self.published_version()  # reconciles through the publish signal, as runtime
            self.assertEqual(
                {
                    str(o.requirement_version.publication.checklist_version_id)
                    for o in self.obligations()
                },
                {v2},
            )
            self.assertEqual(
                ObligationSelection.objects.filter(selection_type="REPLACE").count(), 6
            )
            self.assertEqual(Obligation.objects.count(), 12)
            for table in TABLES:
                for sql in (
                    f"UPDATE {table} SET recorded_at = recorded_at",
                    f"DELETE FROM {table}",
                    f"TRUNCATE TABLE {table}",
                    f"ALTER TABLE {table} ADD COLUMN unsafe INT",
                    f"DROP TABLE {table}",
                ):
                    with (
                        self.subTest(sql=sql),
                        self.assertRaises(DatabaseError) as caught,
                        wrapper.cursor() as cursor,
                    ):
                        cursor.execute(sql)
                    self.assertEqual(caught.exception.args[0], 1142)  # command denied

    def test_runtime_account_can_order_read_and_replay_ad_hoc_checks(self):
        from cya.obligations.adhoc import request_inspection
        from cya.obligations.projections import get_vehicle_obligations_as_of

        self.bind(reconcile=False)
        context = self.context(self.supervisor)
        with self.runtime():

            def order():
                return request_inspection(
                    vehicle_id=str(self.vehicle.pk),
                    items=[{"item_id": str(self.suction.pk), "kit_instance_id": None}],
                    due_at=self.at(hours=6),
                    explanation="Synthetic runtime order.",
                    **context,
                )

            result = order()
            self.assertTrue(order().replayed)
            states = get_vehicle_obligations_as_of(
                str(self.vehicle.pk),
                self.clock.now,
                organization_id=str(self.org.pk),
                start=self.at(),
                end=self.at(days=1),
            )
            self.assertEqual([s.obligation.id for s in states], result.body["obligation_ids"])
            self.assertEqual(states[0].state, "CURRENT")
