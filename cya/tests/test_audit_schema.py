"""MariaDB schema inspection for the append-only contract and command receipt guards."""

from django.db import connection
from django.test import TestCase

from cya.core.audit_schema import (
    COMMAND_RECEIPT_GUARD_EXEMPTIONS,
    FRAMEWORK_TABLES,
    inspect_audit_guards,
    inspect_command_receipt_guards,
)


class PhysicalAuditManifestTests(TestCase):
    def test_every_existing_append_only_table_has_both_guards(self):
        with connection.cursor() as cursor:
            self.assertEqual(
                inspect_audit_guards(cursor),
                {
                    "command_receipt",
                    "audit_event",
                    "role_grant",
                    "checklist_publication",
                    "roster_snapshot",
                    "roster_member",
                    "session_roster_correction",
                    "session_participant",
                    "session_event",
                    "obligation_determination",
                    "vehicle_status_event",
                    "notification_outbox",
                    "notification_delivery_event",
                    "notification_read",
                    "status_request",
                    "requirement_binding",
                    "requirement_version",
                    "obligation",
                    "obligation_selection",
                    "odometer_obligation",
                    "odometer_entry",
                    "engine_hours_entry",
                    "reading_event",
                    "odometer_obligation_link",
                    "odometer_threshold",
                    "check_record",
                    "check_event",
                    "check_obligation_link",
                    "determination_check",
                    "deficiency_affected_item",
                    "deficiency_event",
                    "deficiency_evidence",
                    "deficiency_link",
                    "deficiency_age_rule",
                    "deficiency_acknowledgement_rule",
                    "verification_scope_row",
                    "deficiency_scope_expansion",
                    "check_suspension",
                    "shared_satisfaction_rule_member",
                    "kit_assignment_event",
                },
            )

    def test_every_table_with_command_id_has_receipt_insert_guard(self):
        with connection.cursor() as cursor:
            # Enumerate every table with command_id directly from information_schema
            cursor.execute(
                "SELECT c.TABLE_NAME "
                "FROM information_schema.COLUMNS c "
                "JOIN information_schema.TABLES t "
                "  ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                "WHERE c.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE = 'BASE TABLE' "
                "  AND c.COLUMN_NAME = 'command_id'"
            )
            discovered_tables = {
                row[0] for row in cursor.fetchall() if row[0] not in FRAMEWORK_TABLES
            }

            guarded_tables = inspect_command_receipt_guards(cursor)

            # Table list is not hand-maintained; every table with command_id is covered automatically
            self.assertEqual(guarded_tables, discovered_tables)

            # The four conditions tables from issue 94 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/94) are guaranteed covered and pass
            self.assertTrue(
                {
                    "conditional_rule",
                    "roster_snapshot",
                    "session_roster_correction",
                    "obligation_determination",
                }
                <= guarded_tables
            )
            self.assertIn("audit_event", guarded_tables)

    def test_command_receipt_guard_exemptions_have_documented_reasons(self):
        self.assertIn("audit_event", COMMAND_RECEIPT_GUARD_EXEMPTIONS)
        for table, reason in COMMAND_RECEIPT_GUARD_EXEMPTIONS.items():
            self.assertIsInstance(reason, str)
            self.assertTrue(
                len(reason.strip()) > 10,
                f"Exemption for {table} must provide a descriptive reason",
            )

    def test_unexempted_audit_event_fails_predicate_check(self):
        with connection.cursor() as cursor:
            with self.assertRaisesRegex(
                ValueError,
                r"missing command receipt trigger predicates: \{'audit_event': \['organization_id', 'recorded_at'\]\}",
            ):
                inspect_command_receipt_guards(
                    cursor,
                    exemptions={
                        table: reason
                        for table, reason in COMMAND_RECEIPT_GUARD_EXEMPTIONS.items()
                        if table != "audit_event"
                    },
                )

    def test_empty_exemption_reason_is_refused(self):
        with connection.cursor() as cursor:
            with self.assertRaisesRegex(
                ValueError, r"exemption for table 'audit_event' must provide an explanatory reason"
            ):
                inspect_command_receipt_guards(cursor, exemptions={"audit_event": "   "})

    def test_audit_event_retains_rejecting_receipt_guard(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT TRIGGER_NAME, ACTION_STATEMENT "
                "FROM information_schema.TRIGGERS "
                "WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = 'audit_event' "
                "  AND ACTION_TIMING = 'BEFORE' AND EVENT_MANIPULATION = 'INSERT'"
            )
            triggers = cursor.fetchall()
            statements = " ".join(statement for _, statement in triggers)
            self.assertIn("cya_audit_attribution_bi", [name for name, _ in triggers])
            self.assertIn("SIGNAL SQLSTATE '45000'", statements)
            self.assertIn("command_receipt", statements)
