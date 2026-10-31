import io
from dataclasses import replace
from datetime import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase

from cya.accounts.models import Organization
from cya.authorization.persistence import digest
from cya.authorization.tests.test_persistence import PersistentSetup
from cya.core.audit_commands import Actor, CommandDenied
from cya.core.audit_repository import append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from cya.schedules.commands import (
    DEFAULT_SCHEDULES,
    _append,
    publish_rule,
    seed_default_schedules,
)
from cya.schedules.engine import Rule, due_instants
from cya.schedules.models import ScheduleRule
from cya.schedules.tests.test_engine import instant
from tools.command_identity import worker_operation_id


class SchedulePersistenceTests(PersistentSetup, TransactionTestCase):
    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def rule(self, **values):
        return replace(Rule("UTC", time(6), instant("2026-01-01")), **values)

    def publish(self, rule=None, slot="custom", **overrides):
        context = self.context()
        context.pop("target_id")
        context.update(overrides)
        return publish_rule(slot=slot, rule=rule or self.rule(), **context)

    def test_seed_six_schedules_real_station_zone_and_replay(self):
        Organization.objects.filter(pk=self.org.pk).update(station_zone="America/New_York")
        result = seed_default_schedules(self.org.pk, instant("2026-01-01"))
        self.assertFalse(result.replayed)
        rows = list(ScheduleRule.objects.order_by("slot"))
        self.assertEqual(len(rows), 6)
        for row in rows:
            self.assertEqual(row.zone, "America/New_York")
            self.assertEqual((row.revision, row.early_seconds, row.grace_seconds), (1, 3600, 3600))
            self.assertEqual(row.recorded_at, row.command.recorded_at)
            self.assertIsNone(row.actor_user_id)
            self.assertEqual(row.command.service_identity, "operator.schedules")
        expected = {
            "first-due-0600": "2026-10-23T10:00",
            "first-due-1800": "2026-10-23T22:00",
            "second-due-weekday": "2026-10-23T13:00",
            "third-due-1800": "2026-10-23T22:00",
            "fourth-due-1800": "2026-10-23T22:00",
        }
        for row in rows:
            self.assertEqual(
                list(due_instants(row.as_rule(), instant("2026-10-23"), instant("2026-10-24"))),
                [instant(expected[row.slot])] if row.slot in expected else [],
            )
        counts = self.counts()
        self.assertTrue(seed_default_schedules(self.org.pk, instant("2026-01-01")).replayed)
        self.assertTrue(seed_default_schedules(self.org.pk, instant("2026-02-01")).operation_reused)
        self.assertEqual(counts, self.counts())

    def counts(self):
        return tuple(m.objects.count() for m in (ScheduleRule, CommandReceipt, AuditEvent))

    def seed_receipt(self, job_key, effective_at):
        service = "operator.schedules"
        command_type = "SCHEDULE_SEED"
        return append_receipt(
            organization=self.org,
            operation_id=str(worker_operation_id(str(self.org.pk), service, command_type, job_key)),
            command_type=command_type,
            request_digest=digest({"effective_at": effective_at.isoformat()}),
            response_status=200,
            response_body={"code": "SEEDED", "count": len(DEFAULT_SCHEDULES)},
            correlation_id=str(uuid4()),
            service_identity=service,
            job_key=job_key,
        )

    def test_seed_replays_receipt_with_legacy_job_key(self):
        effective_at = instant("2026-01-01")
        legacy_key = '["legacy-schedule-seed-v1"]'
        service = "operator.schedules"
        with transaction.atomic():
            receipt = self.seed_receipt(legacy_key, effective_at)
            actor = Actor(service_identity=service, job_key=legacy_key)
            for slot, recurrence, wall in DEFAULT_SCHEDULES:
                _append(
                    str(self.org.pk),
                    slot,
                    1,
                    Rule(self.org.station_zone, wall, effective_at, recurrence),
                    receipt,
                    actor,
                )
        before = self.counts()

        result = seed_default_schedules(self.org.pk, effective_at)

        self.assertTrue(result.replayed)
        self.assertEqual(before, self.counts())
        self.assertEqual(ScheduleRule.objects.count(), len(DEFAULT_SCHEDULES))

    def test_seed_refuses_multiple_prior_receipts(self):
        effective_at = instant("2026-01-01")
        with transaction.atomic():
            self.seed_receipt('["legacy-schedule-seed-v1"]', effective_at)
            self.seed_receipt('["other-schedule-seed-v1"]', effective_at)
        before = self.counts()

        with self.assertRaisesMessage(ValueError, "Multiple initial schedule receipts"):
            seed_default_schedules(self.org.pk, effective_at)

        self.assertEqual(before, self.counts())

    def test_seed_refuses_prior_receipt_without_job_key(self):
        effective_at = instant("2026-01-01")
        query = MagicMock()
        query.order_by.return_value.__getitem__.return_value = [
            SimpleNamespace(job_key="", operation_id=str(uuid4()))
        ]
        before = self.counts()

        with (
            patch("cya.schedules.commands.CommandReceipt.objects.filter", return_value=query),
            self.assertRaisesMessage(ValueError, "no worker identity"),
        ):
            seed_default_schedules(self.org.pk, effective_at)

        self.assertEqual(before, self.counts())

    def test_seed_preview_and_bad_input_write_nothing(self):
        counts = self.counts()
        call_command(
            "seed_default_schedules",
            organization=self.org.pk,
            effective_at="2026-01-01T00:00:00Z",
            dry_run=True,
            stdout=io.StringIO(),
        )
        self.assertEqual(counts, self.counts())
        for value in ("garbage", "2026-01-01"):
            with self.assertRaises(CommandError):
                call_command(
                    "seed_default_schedules",
                    organization=self.org.pk,
                    effective_at=value,
                    stdout=io.StringIO(),
                )
        self.assertEqual(counts, self.counts())

    def test_admin_revision_history_replay_and_changed_intent(self):
        op = str(uuid4())
        self.publish(operation_id=op)
        counts = self.counts()
        self.assertTrue(self.publish(operation_id=op).replayed)
        self.assertTrue(
            self.publish(rule=self.rule(early_seconds=0), operation_id=op).operation_reused
        )
        self.assertEqual(counts, self.counts())
        self.publish(
            rule=self.rule(effective_at=instant("2026-02-01"), early_seconds=10, grace_seconds=20)
        )
        self.assertEqual(
            list(
                ScheduleRule.objects.order_by("revision").values_list("revision", "early_seconds")
            ),
            [(1, 3600), (2, 10)],
        )
        with self.assertRaises(ValueError):
            self.publish()
        self.assertEqual(ScheduleRule.objects.count(), 2)
        self.assertTrue(
            AuditEvent.objects.filter(action="SCHEDULE_PUBLISH", outcome="FAILED").exists()
        )

    def test_nonadmin_inactive_and_zone_mismatch_denied(self):
        with self.assertRaises(CommandDenied):
            self.publish(actor_id=self.other.pk)
        with self.assertRaises(ValueError):
            self.publish(rule=self.rule(zone="America/New_York"))
        from cya.accounts.models import User

        User.objects.filter(pk=self.admin.pk).update(active=False)
        with self.assertRaises(CommandDenied):
            self.publish()
        self.assertEqual(ScheduleRule.objects.count(), 0)
        self.assertTrue(
            AuditEvent.objects.filter(action="SCHEDULE_PUBLISH", outcome="DENIED").exists()
        )

    def test_audit_failure_rolls_back_schedule_and_receipt(self):
        receipts = CommandReceipt.objects.count()
        with patch("cya.core.audit_commands.append_audit", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.publish()
        self.assertEqual(ScheduleRule.objects.count(), 0)
        self.assertEqual(CommandReceipt.objects.count(), receipts)

    def test_seed_failure_is_atomic(self):
        from cya.schedules.commands import _append

        calls = 0

        def fail_second(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("synthetic failure")
            return _append(*args)

        receipts = CommandReceipt.objects.count()
        with (
            patch("cya.schedules.commands._append", side_effect=fail_second),
            self.assertRaises(ValueError),
        ):
            seed_default_schedules(self.org.pk, instant("2026-01-01"))
        self.assertEqual(ScheduleRule.objects.count(), 0)
        self.assertEqual(CommandReceipt.objects.count(), receipts)
        seed_default_schedules(self.org.pk, instant("2026-01-01"))
        self.assertEqual(ScheduleRule.objects.count(), 6)

    def test_database_rejects_mutation_invalid_parameters_and_cross_org_evidence(self):
        self.publish()
        row = ScheduleRule.objects.get()
        for action in (
            lambda: ScheduleRule.objects.filter(pk=row.pk).update(early_seconds=0),
            lambda: ScheduleRule.objects.filter(pk=row.pk).delete(),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                action()
        values = {field.attname: getattr(row, field.attname) for field in row._meta.fields}
        other = Organization.objects.create(name="Other Sample Agency", station_zone="UTC")
        for change in (
            {"organization_id": other.pk},
            {"recurrence": "daily"},
            {"revision": 3},
            {"early_seconds": -1},
            {"zone": "America/New_York"},
            {"recurrence": "INTERVAL", "interval_days": None},
            {"actor_user_id": self.other.pk},
        ):
            with (
                self.subTest(change=change),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                ScheduleRule.objects.create(
                    **{**values, "id": str(uuid4()), "slot": "new", **change}
                )
        with connection.cursor() as cursor, self.assertRaises(DatabaseError), transaction.atomic():
            cursor.execute("UPDATE schedule_rule SET wall_time='07:00:00'")

    def test_schedule_command_type_guard(self):
        # 1. Unrelated command type is refused
        with transaction.atomic():
            unrelated = append_receipt(
                organization=self.org,
                actor_user=self.admin,
                operation_id=str(uuid4()),
                command_type="ROLE_CHANGE",
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )
        with self.assertRaises(DatabaseError), transaction.atomic():
            ScheduleRule.objects.create(
                organization=self.org,
                command=unrelated,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
                recorded_at=unrelated.recorded_at,
                slot="guard-test-slot",
                revision=1,
                effective_at=instant("2026-01-01"),
                zone=self.org.station_zone,
                recurrence="DAILY",
                wall_time=time(8),
                early_seconds=3600,
                grace_seconds=3600,
            )

        # 2. SCHEDULE_PUBLISH is accepted
        with transaction.atomic():
            publish_rcpt = append_receipt(
                organization=self.org,
                actor_user=self.admin,
                operation_id=str(uuid4()),
                command_type="SCHEDULE_PUBLISH",
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )
            r1 = ScheduleRule.objects.create(
                organization=self.org,
                command=publish_rcpt,
                actor_user=self.admin,
                actor_roles_at_time=["ADMINISTRATOR"],
                recorded_at=publish_rcpt.recorded_at,
                slot="guard-test-slot-1",
                revision=1,
                effective_at=instant("2026-01-01"),
                zone=self.org.station_zone,
                recurrence="DAILY",
                wall_time=time(8),
                early_seconds=3600,
                grace_seconds=3600,
            )
        self.assertIsNotNone(r1.pk)

        # 3. SCHEDULE_SEED is accepted (worker receipt with actor_user=None)
        service = "operator.schedules"
        job_key = '["test-seed-key"]'
        op_id = str(worker_operation_id(str(self.org.pk), service, "SCHEDULE_SEED", job_key))
        with transaction.atomic():
            seed_rcpt = append_receipt(
                organization=self.org,
                operation_id=op_id,
                service_identity=service,
                job_key=job_key,
                command_type="SCHEDULE_SEED",
                request_digest="0" * 64,
                response_status=200,
                response_body={},
                correlation_id=str(uuid4()),
            )
            r2 = ScheduleRule.objects.create(
                organization=self.org,
                command=seed_rcpt,
                actor_user=None,
                actor_roles_at_time=[],
                recorded_at=seed_rcpt.recorded_at,
                slot="guard-test-slot-2",
                revision=1,
                effective_at=instant("2026-01-01"),
                zone=self.org.station_zone,
                recurrence="DAILY",
                wall_time=time(8),
                early_seconds=3600,
                grace_seconds=3600,
            )
        self.assertIsNotNone(r2.pk)


class ScheduleConcurrencyTests(PersistentSetup, TransactionTestCase):
    def tearDown(self):
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def test_two_connections_publish_one_revision_for_same_effective_instant(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        from django.db import close_old_connections

        barrier = Barrier(2)

        def submit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                publish_rule(
                    organization_id=self.org.pk,
                    actor_id=self.admin.pk,
                    slot="racing",
                    rule=Rule("UTC", time(6), instant("2026-01-01")),
                    operation_id=str(uuid4()),
                    correlation_id=str(uuid4()),
                    request_attempt_id=str(uuid4()),
                )
                return "committed"
            except ValueError:
                return "rejected"
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertCountEqual(results, ["committed", "rejected"])
        self.assertEqual(ScheduleRule.objects.count(), 1)
