"""Physical tenant, attribution, UUID and JSON checks bypass Python validation."""

from importlib import import_module
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from cya.accounts.models import Organization, User
from cya.core.audit_schema import inspect_audit_guards, inspect_command_receipt_guards
from cya.core.models import AuditEvent, CommandReceipt
from tools.audit_enforcement import (
    APPEND_ONLY,
    IMMUTABLE_HEADERS,
    MUTABLE,
    VERSIONED,
    append_only_triggers,
    immutable_header_guard,
)


class AuditIntegrityTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.org,
            idp_issuer="https://idp.example.test",
            idp_subject="member",
            email="member@example.test",
        )
        self.other_user = User.objects.create(
            organization=self.other_org,
            idp_issuer="https://idp.example.test",
            idp_subject="other",
            email="other@example.test",
        )

    def receipt(self, **changes):
        args = dict(
            organization=self.org,
            actor_user=self.user,
            operation_id=str(uuid4()),
            command_type="TEST",
            request_digest="a" * 64,
            result_schema_version=1,
            response_status=200,
            response_body={"saved": True},
            recorded_at=timezone.now(),
            correlation_id=str(uuid4()),
        )
        args.update(changes)
        return CommandReceipt.objects.create(**args)

    def audit(self, **changes):
        args = dict(
            organization=self.org,
            actor_user=self.user,
            request_attempt_id=str(uuid4()),
            action="TEST",
            outcome="SUCCEEDED",
            roles_at_time=["EMT"],
            recorded_at=timezone.now(),
            correlation_id=str(uuid4()),
        )
        args.update(changes)
        return AuditEvent.objects.create(**args)

    def rejects(self, function, **kwargs):
        with self.assertRaises(DatabaseError), transaction.atomic():
            function(**kwargs)

    def test_database_enforces_receipt_tenant_uuid_version_attribution_and_values(self):
        cases = [
            dict(actor_user=self.other_user),
            dict(operation_id="invalid"),
            dict(operation_id=str(uuid4()).upper()),
            dict(operation_id="12345678-1234-5234-9234-123456789abc"),
            dict(service_identity="scheduler"),
            dict(request_digest="X" * 64),
            dict(response_status=503),
            dict(result_schema_version=2),
            dict(response_body=[]),
            dict(command_type="raw request body"),
            dict(command_type="TEST\n"),
            dict(actor_user=None, service_identity="", job_key=""),
            dict(correlation_id="12345678-1234-4234-1234-123456789abc"),
        ]
        for case in cases:
            with self.subTest(fields=tuple(case)):
                self.rejects(self.receipt, **case)
        self.assertEqual(CommandReceipt.objects.count(), 0)

    def test_database_enforces_audit_tenant_actor_and_receipt_attribution(self):
        receipt = self.receipt()
        other_receipt = self.receipt(organization=self.other_org, actor_user=self.other_user)
        for case in [
            dict(actor_user=self.other_user),
            dict(command=other_receipt),
            dict(command=receipt, actor_user=None, roles_at_time=[]),
            dict(command=receipt, correlation_id=str(uuid4())),
            dict(command=receipt, correlation_id=receipt.correlation_id, action="OTHER_ACTION"),
        ]:
            with self.subTest(fields=tuple(case)):
                self.rejects(self.audit, **case)
        accepted = self.audit(command=receipt, correlation_id=receipt.correlation_id)
        self.assertEqual(accepted.command_id, receipt.pk)

    def test_audit_action_command_type_guard(self):
        roster_receipt = self.receipt(command_type="RECORD_SESSION_ROSTER_CORRECTION")
        other_receipt = self.receipt(command_type="CHECKLIST_PUBLISH")

        auxiliary_actions = [
            "ROSTER_CONFLICT_DETECTED",
            "ROSTER_CONFLICT_NOTIFICATION_DELIVERED",
            "ROSTER_CONFLICT_NOTIFICATION_FAILED",
        ]

        # Positive: auxiliary actions accepted with RECORD_SESSION_ROSTER_CORRECTION
        for action in auxiliary_actions:
            with self.subTest(action=action, case="auxiliary_accepted"):
                event = self.audit(
                    command=roster_receipt,
                    correlation_id=roster_receipt.correlation_id,
                    action=action,
                )
                self.assertEqual(event.command_id, roster_receipt.pk)

        # Negative: auxiliary actions rejected with any other command type
        for action in auxiliary_actions:
            with self.subTest(action=action, case="auxiliary_rejected_other_command"):
                self.rejects(
                    self.audit,
                    command=other_receipt,
                    correlation_id=other_receipt.correlation_id,
                    action=action,
                )

        # Negative: unrelated action rejected with RECORD_SESSION_ROSTER_CORRECTION
        self.rejects(
            self.audit,
            command=roster_receipt,
            correlation_id=roster_receipt.correlation_id,
            action="UNRELATED_ACTION",
        )

        # Positive: primary action matching command_type accepted
        event = self.audit(
            command=roster_receipt,
            correlation_id=roster_receipt.correlation_id,
            action="RECORD_SESSION_ROSTER_CORRECTION",
        )
        self.assertEqual(event.command_id, roster_receipt.pk)

        # Positive: auxiliary actions accepted without command receipt
        for action in auxiliary_actions:
            with self.subTest(action=action, case="auxiliary_without_receipt"):
                event = self.audit(command=None, action=action)
                self.assertIsNone(event.command_id)

    def test_audit_closed_values_targets_and_snapshot_shape(self):
        cases = [
            dict(outcome="unknown"),
            dict(outcome="DENIED"),
            dict(action="TEST\n"),
            dict(target_type="USER", target_id=str(self.other_user.pk)),
            dict(target_type="ORGANIZATION", target_id=str(self.other_org.pk)),
            dict(target_type="UNREGISTERED", target_id=str(self.user.pk)),
            dict(roles_at_time={}),
            dict(roles_at_time=["ROOT"]),
            dict(actor_user=None, roles_at_time=["EMT"]),
            dict(target_type="USER"),
            dict(target_id=str(uuid4())),
            dict(target_type="USER", target_id="invalid"),
            dict(reason_code="raw exception"),
            dict(new_snapshot={}),
            dict(new_snapshot={"schema_version": 2, "values": {}}),
            dict(new_snapshot={"schema_version": 1, "values": []}),
            dict(new_snapshot={"schema_version": "1", "values": {}}),
            dict(new_snapshot={"schema_version": 1, "values": {}, "extra": "field"}),
        ]
        for case in cases:
            with self.subTest(fields=tuple(case)):
                self.rejects(self.audit, **case)
        self.assertEqual(AuditEvent.objects.count(), 0)

    # Tables added to the live manifest after the frozen baseline, each with its own feature
    # migration installing guards and grants (the baseline migration's docstring requires that).
    # Listing one here is a deliberate act: an undeclared addition or reclassification fails.
    AFTER_BASELINE = {
        "A": {
            "requirement_binding",  # issue 59 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/59): binds a vehicle's template to a schedule slot
            "shared_satisfaction_rule_member",  # issue 74 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/74): audited rule membership evidence
            # issue 97 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/97): classified now so the contracts catalogue and the manifest agree; the
            # table, its guards and grants arrive with the implementation (issue 92 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/92))
            "kit_assignment_event",
            "odometer_threshold",  # issue 79 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/79): administrator-set plausibility threshold revisions
            "notification_read",  # issue 147 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/147): the recipient's read mark on an in-app item
            "deficiency_age_rule",  # issue 150 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/150): administrator-set age highlighting revisions
            # issue 151 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/151): administrator-set acknowledgement maximum
            "deficiency_acknowledgement_rule",
            "deficiency_scope_expansion",  # issue 156 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/156): supervisor-added verification items
        },
        # issue 147 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/147): administrator-set mandatory event/channel pairs
        "M": {"notification_policy"},
        # issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80): administrator-configured severity levels
        "V": {"deficiency_severity"},
    }

    def test_manifest_baseline_is_migration_owned_and_matches_live_classes(self):
        baseline = import_module("cya.core.migrations.0004_audit_integrity").TABLE_CLASSES
        self.assertEqual(len(baseline["A"]), 38)
        for kind, current in (("A", APPEND_ONLY), ("M", MUTABLE), ("V", VERSIONED)):
            self.assertEqual(set(baseline[kind]) | self.AFTER_BASELINE[kind], current)
            self.assertFalse(set(baseline[kind]) & self.AFTER_BASELINE[kind])


class AuditSchemaLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.addCleanup(call_command, "flush", verbosity=0, interactive=False, reset_sequences=True)

    def test_future_unknown_missing_and_noop_triggers_fail_closed(self):
        with connection.cursor() as cursor:
            try:
                cursor.execute("CREATE TABLE unclassified_probe (id INTEGER PRIMARY KEY)")
                with self.assertRaisesRegex(ValueError, "unclassified"):
                    inspect_audit_guards(cursor)
            finally:
                cursor.execute("DROP TABLE IF EXISTS unclassified_probe")
            try:
                cursor.execute("DROP TRIGGER cya_reading_event_bu")
                cursor.execute("DROP TRIGGER cya_reading_event_bd")
                with self.assertRaisesRegex(ValueError, "missing"):
                    inspect_audit_guards(cursor)
                for op, suffix in (("UPDATE", "bu"), ("DELETE", "bd")):
                    cursor.execute(
                        f"CREATE TRIGGER probe_{suffix} BEFORE {op} ON reading_event FOR EACH ROW SET @probe=1"
                    )
                with self.assertRaisesRegex(ValueError, "missing"):
                    inspect_audit_guards(cursor)
                for suffix in ("bu", "bd"):
                    cursor.execute(f"DROP TRIGGER probe_{suffix}")
                for sql in append_only_triggers("reading_event"):
                    cursor.execute(sql)
                self.assertEqual(
                    inspect_audit_guards(cursor),
                    {
                        "role_grant",
                        "audit_event",
                        "command_receipt",
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
            finally:
                for suffix in ("bu", "bd"):
                    cursor.execute(f"DROP TRIGGER IF EXISTS probe_{suffix}")
                    cursor.execute(f"DROP TRIGGER IF EXISTS cya_reading_event_{suffix}")
                for sql in append_only_triggers("reading_event"):
                    cursor.execute(sql)

    def test_header_template_rejects_case_only_and_null_to_value_changes(self):
        # A temporary comparison probe on a lock table not yet built, not committed feature schema.
        columns = ("id", "organization_id") + IMMUTABLE_HEADERS["vehicle_lock"]
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    "CREATE TABLE vehicle_lock ("
                    + ",".join(f"`{name}` TEXT NULL" for name in columns)
                    + ") COLLATE utf8mb4_unicode_ci"
                )
                cursor.execute(immutable_header_guard("vehicle_lock"))
                cursor.execute(
                    "INSERT INTO vehicle_lock (id,organization_id,vehicle_id) "
                    "VALUES ('synthetic','synthetic','Name')"
                )
                cursor.execute("INSERT INTO vehicle_lock (id,organization_id) VALUES ('empty','o')")
                for sql in (
                    "UPDATE vehicle_lock SET vehicle_id='name' WHERE id='synthetic'",
                    "UPDATE vehicle_lock SET vehicle_id='x' WHERE id='empty'",
                ):
                    with self.assertRaises(DatabaseError) as caught:
                        cursor.execute(sql)
                    self.assertEqual(caught.exception.args[0], 1644)
                cursor.execute("UPDATE vehicle_lock SET vehicle_id='Name' WHERE id='synthetic'")
                cursor.execute("SELECT vehicle_id FROM vehicle_lock WHERE id='empty'")
                self.assertEqual(cursor.fetchone(), (None,))
            finally:
                cursor.execute("DROP TABLE IF EXISTS vehicle_lock")

    def test_integrity_migrations_reverse_and_reapply_on_empty_disposable_schema(self):
        targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            MigrationExecutor(connection).migrate([("cya_core", "0002_audit_append_only")])
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='cya_audit_attribution_bi'"
                )
                self.assertEqual(cursor.fetchone()[0], 0)
        finally:
            MigrationExecutor(connection).migrate(targets)
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
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_NAME IN ('receipt_actor_org_fk','audit_actor_org_fk','audit_command_org_fk')"
            )
            self.assertEqual(cursor.fetchone()[0], 3)

    def test_command_receipt_guard_lifecycle(self):
        with connection.cursor() as cursor:
            # 1. A table with command_id and no trigger fails
            try:
                cursor.execute(
                    "CREATE TABLE cmd_probe ("
                    "id INT PRIMARY KEY, "
                    "organization_id CHAR(36), "
                    "command_id CHAR(36), "
                    "actor_user_id CHAR(36), "
                    "recorded_at DATETIME(6)"
                    ")"
                )
                with self.assertRaisesRegex(
                    ValueError, r"missing command receipt insert guards: \['cmd_probe'\]"
                ):
                    inspect_command_receipt_guards(cursor)

                # 2. A trigger that does not reference command_receipt fails
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW SET @dummy = 1"
                )
                with self.assertRaisesRegex(
                    ValueError, r"missing command receipt insert guards: \['cmd_probe'\]"
                ):
                    inspect_command_receipt_guards(cursor)
                cursor.execute("DROP TRIGGER cmd_probe_bi")

                # 2b. A no-op trigger that merely assigns keyword text fails (Finding 2)
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW SET @dummy = 'command_receipt organization_id actor_user_id recorded_at command_type'"
                )
                with self.assertRaisesRegex(
                    ValueError, r"missing command receipt insert guards: \['cmd_probe'\]"
                ):
                    inspect_command_receipt_guards(cursor)
                cursor.execute("DROP TRIGGER cmd_probe_bi")

                # 2c. A non-enforcing receipt lookup and unreachable rejection fails
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW BEGIN "
                    "IF FALSE THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'unreachable'; END IF; "
                    "SET @dummy = (SELECT COUNT(*) FROM command_receipt r "
                    "WHERE r.id = NEW.command_id AND r.organization_id = NEW.organization_id "
                    "AND r.actor_user_id <=> NEW.actor_user_id AND r.recorded_at = NEW.recorded_at "
                    "AND r.command_type = 'CMD_PROBE'); END"
                )
                with self.assertRaisesRegex(
                    ValueError, r"missing command receipt insert guards: \['cmd_probe'\]"
                ):
                    inspect_command_receipt_guards(cursor)
                cursor.execute("DROP TRIGGER cmd_probe_bi")

                # 2d. A trigger with receipt binding in the signal condition but predicates outside in an inert statement fails predicate check
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id) THEN "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'missing'; END IF; "
                    "SET @dummy = (SELECT COUNT(*) FROM command_receipt r "
                    "WHERE r.organization_id = NEW.organization_id "
                    "AND r.actor_user_id <=> NEW.actor_user_id AND r.recorded_at = NEW.recorded_at "
                    "AND r.command_type = 'CMD_PROBE'); END"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    r"missing command receipt trigger predicates: \{'cmd_probe': \['organization_id', 'actor', 'recorded_at', 'command_type'\]\}",
                ):
                    inspect_command_receipt_guards(cursor)
                cursor.execute("DROP TRIGGER cmd_probe_bi")

                # 3. A trigger referencing command_receipt but missing predicates fails
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id) THEN "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'missing'; END IF; END"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    r"missing command receipt trigger predicates: \{'cmd_probe': \['organization_id', 'actor', 'recorded_at', 'command_type'\]\}",
                ):
                    inspect_command_receipt_guards(cursor)
                cursor.execute("DROP TRIGGER cmd_probe_bi")

                # 4. A trigger with all required predicates passes
                cursor.execute(
                    "CREATE TRIGGER cmd_probe_bi BEFORE INSERT ON cmd_probe "
                    "FOR EACH ROW BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id "
                    "AND r.organization_id = NEW.organization_id "
                    "AND r.actor_user_id <=> NEW.actor_user_id "
                    "AND r.recorded_at = NEW.recorded_at "
                    "AND r.command_type = 'CMD_PROBE') THEN "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'attribution mismatch'; END IF; END"
                )
                guarded = inspect_command_receipt_guards(cursor)
                self.assertIn("cmd_probe", guarded)
            finally:
                cursor.execute("DROP TABLE IF EXISTS cmd_probe")

            # 5. Worker columns (service_identity, job_key) require worker predicates
            try:
                cursor.execute(
                    "CREATE TABLE cmd_worker_probe ("
                    "id INT PRIMARY KEY, "
                    "organization_id CHAR(36), "
                    "command_id CHAR(36), "
                    "actor_user_id CHAR(36), "
                    "service_identity VARCHAR(64), "
                    "job_key VARCHAR(64), "
                    "recorded_at DATETIME(6)"
                    ")"
                )
                cursor.execute(
                    "CREATE TRIGGER cmd_worker_probe_bi BEFORE INSERT ON cmd_worker_probe "
                    "FOR EACH ROW BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM command_receipt r WHERE r.id = NEW.command_id "
                    "AND r.organization_id = NEW.organization_id "
                    "AND r.actor_user_id <=> NEW.actor_user_id "
                    "AND r.recorded_at = NEW.recorded_at "
                    "AND r.command_type = 'CMD_PROBE') THEN "
                    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'attribution mismatch'; END IF; END"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    r"missing command receipt trigger predicates: \{'cmd_worker_probe': \['service_identity', 'job_key'\]\}",
                ):
                    inspect_command_receipt_guards(cursor)
            finally:
                cursor.execute("DROP TABLE IF EXISTS cmd_worker_probe")

            # 6. An exempt table that lacks a rejecting receipt guard fails (Finding 1)
            try:
                cursor.execute(
                    "CREATE TABLE exempt_probe (id INT PRIMARY KEY, command_id CHAR(36))"
                )
                exemptions = {
                    "exempt_probe": "deliberately exempt probe table",
                }
                with self.assertRaisesRegex(
                    ValueError, r"missing command receipt insert guards: \['exempt_probe'\]"
                ):
                    inspect_command_receipt_guards(cursor, exemptions=exemptions)
            finally:
                cursor.execute("DROP TABLE IF EXISTS exempt_probe")
