"""Actual runtime grants and schedule enforcement against disposable MariaDB."""

import io
import os
from datetime import time
from unittest import skipUnless
from uuid import uuid4

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase

from cya.authorization.roles import grant_initial_crew
from cya.schedules.commands import publish_rule, seed_default_schedules
from cya.schedules.engine import Rule
from cya.schedules.models import ScheduleRule
from cya.schedules.tests.test_engine import instant
from cya.tests import test_audit_runtime_db as runtime_tests


@skipUnless(os.environ.get("CYA_DB_SECURITY_TESTS") == "1", "requires disposable DBA principal")
class ScheduleRuntimeTests(TransactionTestCase):
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
        # Exercise the supported installer rather than manually granting the
        # new table; replay is safe and its inventory must include schedules.
        for _ in range(2):
            call_command("audit_runtime_grants", user=self.username, host="%", stdout=io.StringIO())

    def test_runtime_publish_and_forbidden_update_delete_ddl(self):
        with self.runtime() as wrapper:
            result = publish_rule(
                organization_id=self.org.pk,
                actor_id=self.user.pk,
                slot="runtime",
                rule=Rule("UTC", time(6), instant("2026-01-01")),
                operation_id=str(uuid4()),
                correlation_id=str(uuid4()),
                request_attempt_id=str(uuid4()),
            )
            self.assertEqual(result.body["revision"], 1)
            self.assertEqual(ScheduleRule.objects.count(), 1)
            for sql in (
                "UPDATE schedule_rule SET early_seconds=0",
                "DELETE FROM schedule_rule",
                "TRUNCATE TABLE schedule_rule",
                "ALTER TABLE schedule_rule ADD COLUMN unsafe INT",
            ):
                with (
                    self.subTest(sql=sql),
                    self.assertRaises(DatabaseError) as caught,
                    wrapper.cursor() as cursor,
                ):
                    cursor.execute(sql)
                self.assertEqual(caught.exception.args[0], 1142)
            with self.assertRaises(PermissionError):
                seed_default_schedules(self.org.pk, instant("2026-01-01"))
