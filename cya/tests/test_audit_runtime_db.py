"""Effective MariaDB runtime grants, using disposable principals (never live data).

CI opts in with CYA_DB_SECURITY_TESTS=1 and a disposable DBA/schema-owner.
Normal development schema owners need not have CREATE USER or GRANT OPTION.
"""

import copy
import io
import os
import secrets
from contextlib import contextmanager
from unittest import skipUnless
from uuid import uuid4

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import DatabaseError, connections, transaction
from django.test import TransactionTestCase, override_settings

from cya.accounts.models import Organization, User
from cya.authorization.models import RoleGrant
from cya.authorization.roles import grant_initial_emt
from cya.core.audit_commands import Actor, Plan, run_command
from cya.core.audit_repository import append_audit, append_receipt
from cya.core.audit_schema import inspect_audit_guards
from cya.core.models import AuditEvent, CommandReceipt
from tools.audit_grants import quote_sql_string, runtime_grants
from tools.audit_runtime_verify import check_runtime_connection


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class RuntimePrivilegeTests(TransactionTestCase):
    def setUp(self):
        self.addCleanup(call_command, "flush", verbosity=0, interactive=False, reset_sequences=True)
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = User.objects.create(
            organization=self.org,
            idp_issuer="https://idp.example.test",
            idp_subject="synthetic-member",
            email="member@example.test",
        )
        self.owner = connections["default"]
        self.database = self.owner.settings_dict["NAME"]
        self.username = "cya_test_" + uuid4().hex[:16]
        self.password = secrets.token_hex(24)
        self.account = quote_sql_string(self.username) + "@'%'"
        with self.owner.cursor() as cursor:
            cursor.execute(
                "CREATE USER %s@%s IDENTIFIED BY %s", [self.username, "%", self.password]
            )
        self.addCleanup(self.drop_account)
        with self.owner.cursor() as cursor:
            present = inspect_audit_guards(cursor)
            for sql in runtime_grants(
                database=self.database,
                username=self.username,
                host="%",
                existing_tables=present | {"organization", "user"},
            ):
                cursor.execute(sql)
            cursor.execute(f"GRANT SELECT ON `{self.database}`.`organization` TO {self.account}")
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE ON `{self.database}`.`user` TO {self.account}"
            )

    def drop_account(self):
        with self.owner.cursor() as cursor:
            cursor.execute(f"DROP USER IF EXISTS {self.account}")

    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    @contextmanager
    def runtime(self, *, verify=True):
        config = copy.deepcopy(self.owner.settings_dict)
        config.update(USER=self.username, PASSWORD=self.password)
        wrapper = self.owner.__class__(config, alias="default")
        connections["default"] = wrapper
        try:
            with override_settings(CYA_DATABASE_ROLE="runtime" if verify else "schema_owner"):
                yield wrapper
        finally:
            wrapper.close()
            connections["default"] = self.owner

    def receipt(self):
        return append_receipt(
            organization=self.org,
            actor_user=self.user,
            operation_id=str(uuid4()),
            command_type="TEST",
            request_digest="a" * 64,
            response_status=200,
            response_body={"saved": True},
            correlation_id=str(uuid4()),
        )

    def test_actual_show_grants_and_orm_positive_appends(self):
        with self.runtime() as wrapper:
            call_command("verify_audit_runtime", stdout=io.StringIO())
            with wrapper.cursor() as cursor:
                check_runtime_connection(database=self.database, cursor=cursor)
                cursor.execute("SELECT CURRENT_USER()")
                self.assertEqual(cursor.fetchone()[0], self.username + "@%")
            with transaction.atomic():
                receipt = self.receipt()
                audit = append_audit(
                    organization=self.org,
                    actor_user=self.user,
                    command=receipt,
                    request_attempt_id=str(uuid4()),
                    action="TEST",
                    outcome="SUCCEEDED",
                    correlation_id=receipt.correlation_id,
                    roles_at_time=["EMT"],
                )
            self.assertTrue(CommandReceipt.objects.filter(pk=receipt.pk).exists())
            self.assertTrue(AuditEvent.objects.filter(pk=audit.pk).exists())

    def test_full_audited_command_commits_under_runtime_credentials(self):
        with self.runtime():

            def authorize(org):
                return Actor(User.objects.select_for_update().get(pk=self.user.pk), ("EMT",))

            def prepare(actor):
                def apply(receipt, actor):
                    User.objects.filter(pk=self.user.pk).update(display_name="Saved Synthetic")

                return Plan({"saved": True}, apply)

            result = run_command(
                organization_id=str(self.org.pk),
                operation_id=str(uuid4()),
                command_type="PROFILE_UPDATE",
                request_digest="a" * 64,
                correlation_id=str(uuid4()),
                request_attempt_id=str(uuid4()),
                authorize=authorize,
                prepare=prepare,
            )
            self.assertEqual(result.body, {"saved": True})
            self.assertEqual(AuditEvent.objects.get().outcome, "SUCCEEDED")

    def test_role_history_runtime_insert_and_write_privilege_refusal(self):
        with self.runtime() as wrapper:
            with transaction.atomic():
                grant_initial_emt(user=self.user, command_id=str(uuid4()))
            grant = RoleGrant.objects.get(user=self.user)
            self.assertEqual(grant.role, "CREW")
            for sql in (
                "UPDATE role_grant SET role='ADMINISTRATOR'",
                "DELETE FROM role_grant",
                "REPLACE INTO role_grant SELECT * FROM role_grant",
                "INSERT INTO role_grant SELECT * FROM role_grant ON DUPLICATE KEY UPDATE role='ADMINISTRATOR'",
                "TRUNCATE TABLE role_grant",
                "ALTER TABLE role_grant ADD COLUMN unsafe INT",
            ):
                with self.subTest(sql=sql), self.assertRaises(DatabaseError) as caught:
                    with wrapper.cursor() as cursor:
                        cursor.execute(sql)
                self.assertIn(caught.exception.args[0], (1142, 1143))

    def test_grant_command_dry_run_is_read_only_and_apply_is_repeatable(self):
        with self.owner.cursor() as cursor:
            cursor.execute(f"REVOKE INSERT ON `{self.database}`.`audit_event` FROM {self.account}")
        out = io.StringIO()
        call_command("audit_runtime_grants", user=self.username, host="%", dry_run=True, stdout=out)
        self.assertIn("GRANT SELECT, INSERT", out.getvalue())
        with self.runtime() as wrapper:
            with self.assertRaises(ImproperlyConfigured):
                wrapper.ensure_connection()
        for _ in range(2):
            call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())
        with self.runtime() as wrapper:
            wrapper.ensure_connection()
            self.assertIsNotNone(wrapper.connection)

    def test_runtime_update_delete_replace_upsert_and_ddl_are_privilege_denied(self):
        with self.runtime() as wrapper:
            with transaction.atomic():
                receipt = self.receipt()
                audit = append_audit(
                    organization=self.org,
                    request_attempt_id=str(uuid4()),
                    action="TEST",
                    outcome="DENIED",
                    reason_code="FORBIDDEN",
                    correlation_id=str(uuid4()),
                    roles_at_time=[],
                )
            for model, row in ((CommandReceipt, receipt), (AuditEvent, audit)):
                for operation in (
                    lambda model=model, row=row: model.objects.filter(pk=row.pk).update(
                        recorded_at=row.recorded_at
                    ),
                    lambda model=model, row=row: model.objects.filter(pk=row.pk).delete(),
                    lambda row=row: row.save(),
                ):
                    with self.subTest(table=model._meta.db_table):
                        with self.assertRaises(DatabaseError) as caught:
                            operation()
                        self.assertEqual(caught.exception.args[0], 1142)
                self.assertTrue(model.objects.filter(pk=row.pk).exists())
            statements = [
                "DELETE FROM command_receipt",
                "DELETE FROM audit_event",
                "TRUNCATE TABLE audit_event",
                "ALTER TABLE audit_event ADD COLUMN unsafe INT",
                "DROP TABLE audit_event",
                "CREATE TABLE unsafe (id INT)",
                "CREATE TRIGGER unsafe BEFORE DELETE ON audit_event FOR EACH ROW SET @unsafe=1",
                f"GRANT UPDATE ON `{self.database}`.`audit_event` TO {self.account}",
                "REPLACE INTO command_receipt SELECT * FROM command_receipt",
                "INSERT INTO command_receipt SELECT * FROM command_receipt ON DUPLICATE KEY UPDATE command_type='TEST'",
            ]
            for sql in statements:
                with self.subTest(operation=sql.split()[0]):
                    with self.assertRaises(DatabaseError) as caught:
                        with wrapper.cursor() as cursor:
                            cursor.execute(sql)
                    self.assertIn(caught.exception.args[0], (1044, 1142, 1143, 1227))
            self.assertEqual(CommandReceipt.objects.count(), 1)
            self.assertEqual(AuditEvent.objects.count(), 1)

    def test_existing_identity_guard_allows_audited_mutable_fields_not_retargeting(self):
        with self.runtime():
            with transaction.atomic():
                User.objects.filter(pk=self.user.pk).update(display_name="Synthetic Updated")
                append_audit(
                    organization=self.org,
                    actor_user=self.user,
                    request_attempt_id=str(uuid4()),
                    action="PROFILE_UPDATE",
                    outcome="SUCCEEDED",
                    correlation_id=str(uuid4()),
                    roles_at_time=["EMT"],
                    target_type="USER",
                    target_id=str(self.user.pk),
                    prior_snapshot={"schema_version": 1, "values": {"display_name": ""}},
                    new_snapshot={
                        "schema_version": 1,
                        "values": {"display_name": "Synthetic Updated"},
                    },
                )
            with self.assertRaises(DatabaseError) as caught:
                User.objects.filter(pk=self.user.pk).update(idp_subject="retargeted")
            self.assertEqual(caught.exception.args[0], 1644)
            with self.assertRaises(DatabaseError):
                User.objects.filter(pk=self.user.pk).delete()

    def test_connection_refuses_global_schema_table_and_delegation_grants(self):
        cases = [
            ("UPDATE", "*.*"),
            ("INSERT", f"`{self.database}`.*"),
            ("DELETE", f"`{self.database}`.`audit_event`"),
            ("ALTER", f"`{self.database}`.`audit_event`"),
        ]
        for privilege, scope in cases:
            with self.subTest(privilege=privilege, scope=scope):
                with self.owner.cursor() as cursor:
                    cursor.execute(f"GRANT {privilege} ON {scope} TO {self.account}")
                try:
                    with self.runtime() as wrapper:
                        with self.assertRaises(ImproperlyConfigured):
                            wrapper.ensure_connection()
                        self.assertIsNone(wrapper.connection)
                finally:
                    with self.owner.cursor() as cursor:
                        cursor.execute(f"REVOKE {privilege} ON {scope} FROM {self.account}")
        with self.owner.cursor() as cursor:
            cursor.execute(
                f"GRANT SELECT, INSERT ON `{self.database}`.`audit_event` TO {self.account} WITH GRANT OPTION"
            )
        with self.runtime() as wrapper:
            with self.assertRaises(ImproperlyConfigured):
                wrapper.ensure_connection()

    def test_assigned_role_even_when_inactive_is_rejected(self):
        role = "cya_role_" + uuid4().hex[:16]
        try:
            with self.owner.cursor() as cursor:
                cursor.execute(f"CREATE ROLE `{role}`")
                cursor.execute(f"GRANT UPDATE ON `{self.database}`.`audit_event` TO `{role}`")
                cursor.execute(f"GRANT `{role}` TO {self.account}")
            with self.runtime() as wrapper:
                with self.assertRaises(ImproperlyConfigured):
                    wrapper.ensure_connection()
            # Explicitly activating a role is rejected independently of SHOW GRANTS.
            with self.runtime(verify=False) as wrapper:
                with wrapper.cursor() as cursor:
                    cursor.execute(f"SET ROLE `{role}`")
                    with self.assertRaisesRegex(ValueError, "roles"):
                        check_runtime_connection(database=self.database, cursor=cursor)
        finally:
            with self.owner.cursor() as cursor:
                cursor.execute(f"DROP ROLE IF EXISTS `{role}`")
