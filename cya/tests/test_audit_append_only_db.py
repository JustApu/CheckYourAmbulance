"""Real MariaDB enforcement; schema-owner privileges cannot bypass row triggers.

These are not effective-runtime-grant tests. The owner can still change DDL;
issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23) must separately remove those powers from the application account.
"""

from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.core.models import AuditEvent, CommandReceipt


class AuditAppendOnlyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        cls.user = User.objects.create(
            organization=cls.org,
            idp_issuer="https://identity.example.test",
            idp_subject="synthetic-subject",
            email="member@example.test",
            display_name="Synthetic Member",
        )
        cls.receipt = CommandReceipt.objects.create(
            organization=cls.org,
            operation_id=str(uuid4()),
            actor_user=cls.user,
            command_type="TEST_COMMAND",
            request_digest="a" * 64,
            result_schema_version=1,
            response_status=200,
            response_body={"status": "SAVED"},
            recorded_at=timezone.now(),
            correlation_id=str(uuid4()),
        )
        # No receipt FK here so ORM deletion reaches the receipt's DELETE trigger,
        # rather than stopping earlier at Django's PROTECT collector.
        cls.audit = AuditEvent.objects.create(
            organization=cls.org,
            command=None,
            request_attempt_id=str(uuid4()),
            actor_user=cls.user,
            roles_at_time=["EMT"],
            action="TEST_COMMAND",
            recorded_at=timezone.now(),
            correlation_id=str(uuid4()),
            outcome="SUCCEEDED",
        )

    def assert_trigger_rejects(self, operation):
        with self.assertRaises(DatabaseError) as caught:
            with transaction.atomic():
                operation()
        self.assertEqual(caught.exception.args[0], 1644)
        self.assertIn("append-only table", str(caught.exception))

    def test_schema_accepts_receipt_and_command_linked_audit_inserts(self):
        linked = AuditEvent.objects.create(
            organization=self.org,
            command=self.receipt,
            request_attempt_id=str(uuid4()),
            actor_user=self.user,
            roles_at_time=["EMT"],
            action="TEST_COMMAND",
            recorded_at=timezone.now(),
            correlation_id=self.receipt.correlation_id,
            outcome="SUCCEEDED",
        )
        self.assertEqual(linked.command_id, self.receipt.pk)
        self.assertEqual(
            CommandReceipt.objects.get(pk=self.receipt.pk).response_body, {"status": "SAVED"}
        )
        self.assertEqual(AuditEvent.objects.filter(command=self.receipt).count(), 1)

    def test_all_four_triggers_are_installed_as_before_row_guards(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING "
                "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE() "
                "AND EVENT_OBJECT_TABLE IN ('command_receipt', 'audit_event') "
                "AND EVENT_MANIPULATION IN ('UPDATE', 'DELETE')"
            )
            self.assertEqual(
                set(cursor.fetchall()),
                {
                    (table, action, "BEFORE")
                    for table in ("command_receipt", "audit_event")
                    for action in ("UPDATE", "DELETE")
                },
            )

    def test_direct_sql_updates_and_deletes_are_rejected_and_rows_survive(self):
        for row, column, original in (
            (self.receipt, "command_type", "TEST_COMMAND"),
            (self.audit, "action", "TEST_COMMAND"),
        ):
            table = row._meta.db_table
            with self.subTest(table=table):

                def update(table=table, column=column, row=row):
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE `{table}` SET `{column}` = %s WHERE id = %s",
                            ["CHANGED", row.pk],
                        )

                def delete(table=table, row=row):
                    with connection.cursor() as cursor:
                        cursor.execute(f"DELETE FROM `{table}` WHERE id = %s", [row.pk])

                self.assert_trigger_rejects(update)
                self.assert_trigger_rejects(delete)
                row.refresh_from_db()
                self.assertEqual(getattr(row, column), original)

    def test_orm_update_save_and_delete_are_rejected(self):
        for row, column in ((self.receipt, "command_type"), (self.audit, "action")):
            with self.subTest(table=row._meta.db_table):
                rows = type(row).objects.filter(pk=row.pk)
                self.assert_trigger_rejects(
                    lambda rows=rows, column=column: rows.update(**{column: "CHANGED"})
                )
                setattr(row, column, "CHANGED")
                self.assert_trigger_rejects(
                    lambda row=row, column=column: row.save(update_fields=[column])
                )
                self.assert_trigger_rejects(rows.delete)
                row.refresh_from_db()
                self.assertEqual(getattr(row, column), "TEST_COMMAND")
