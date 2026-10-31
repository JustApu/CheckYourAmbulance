"""Real transaction, replay and concurrency checks over synthetic profile writes.

Profile writes stand in for future domain effects; no future tables or routes
are fabricated. Policy callbacks below are test-only, not production auth.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, connections, transaction
from django.test import TransactionTestCase

from cya.accounts.models import Organization, User
from cya.core.audit_commands import (
    Actor,
    CommandDenied,
    OperationConflict,
    OutcomeUnknown,
    Plan,
    RetryLater,
    run_command,
)
from cya.core.audit_transactions import AuditPersistenceFailure
from cya.core.models import AuditEvent, CommandReceipt
from tools.command_identity import worker_operation_id


class AuditCommandTests(TransactionTestCase):
    def setUp(self):
        self.addCleanup(call_command, "flush", verbosity=0, interactive=False, reset_sequences=True)
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.user = self.new_user("first")
        self.other = self.new_user("second")
        self.operation = str(uuid4())
        self.calls = []
        self.revoked = False

    def new_user(self, subject):
        return User.objects.create(
            organization=self.org,
            idp_issuer="https://idp.example.test",
            idp_subject=subject,
            email=subject + "@example.test",
        )

    def authorize(self, org):
        user = User.objects.select_for_update().get(pk=self.user.pk, organization=org)
        actor = Actor(user, ("EMT",))
        if self.revoked:
            raise CommandDenied(Actor(user))
        return actor

    def prepare(self, actor):
        def apply(receipt, actor):
            self.calls.append("effect")
            self.assertTrue(CommandReceipt.objects.filter(pk=receipt.pk).exists())
            User.objects.filter(pk=self.user.pk).update(display_name="Synthetic Updated")

        return Plan(
            {"saved": True},
            apply,
            target_type="USER",
            target_id=str(self.user.pk),
            prior_snapshot={"schema_version": 1, "values": {"display_name": ""}},
            new_snapshot={"schema_version": 1, "values": {"display_name": "Synthetic Updated"}},
        )

    def command(self, **overrides):
        args = dict(
            organization_id=str(self.org.pk),
            operation_id=self.operation,
            command_type="PROFILE_UPDATE",
            request_digest="a" * 64,
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
            authorize=self.authorize,
            prepare=self.prepare,
        )
        args.update(overrides)
        return run_command(**args)

    def test_commit_replay_and_changed_request_return_original_acknowledgement(self):
        result = self.command()
        replay = self.command(request_digest="b" * 64)
        self.assertFalse(result.replayed)
        self.assertTrue(replay.replayed)
        self.assertTrue(replay.operation_reused)
        self.assertEqual((result.body, result.correlation_id), (replay.body, replay.correlation_id))
        self.assertEqual(self.calls, ["effect"])
        audit = AuditEvent.objects.get()
        self.assertEqual(audit.roles_at_time, ["EMT"])
        self.assertEqual(audit.target_id, str(self.user.pk))
        self.assertEqual(audit.new_snapshot["values"]["display_name"], "Synthetic Updated")
        self.assertEqual(audit.command_id, CommandReceipt.objects.get().pk)

    def test_replay_rechecks_access_and_does_not_expose_other_actors_result(self):
        self.command()
        self.revoked = True
        with self.assertRaises(CommandDenied):
            self.command()
        self.revoked = False
        with self.assertLogs("cya.core.audit_commands", level="ERROR"):
            with self.assertRaises(OperationConflict):
                self.command(
                    authorize=lambda org: Actor(
                        User.objects.select_for_update().get(pk=self.other.pk), ("EMT",)
                    )
                )
        self.assertEqual(self.calls, ["effect"])
        self.assertEqual(CommandReceipt.objects.count(), 1)
        self.assertEqual(
            set(AuditEvent.objects.values_list("outcome", flat=True)),
            {"SUCCEEDED", "DENIED", "FAILED"},
        )

    def test_business_and_audit_failures_roll_back_receipt_and_effects(self):
        def bad_apply(receipt, actor):
            User.objects.filter(pk=self.user.pk).update(display_name="Must roll back")
            raise ValueError("synthetic private error body")

        with self.assertRaises(ValueError):
            self.command(prepare=lambda actor: Plan({"saved": True}, bad_apply))
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "")
        audit = AuditEvent.objects.get()
        self.assertEqual((audit.outcome, audit.reason_code), ("FAILED", "COMMAND_FAILED"))
        self.assertIsNone(audit.command_id)
        self.assertIsNone(audit.new_snapshot)

        # A malformed success audit fails after a real business UPDATE.
        def invalid_audit(actor):
            base = self.prepare(actor)
            return Plan(
                base.response_body, base.apply, target_type="bad code", target_id=str(self.user.pk)
            )

        with self.assertRaises(ValidationError):
            self.command(prepare=invalid_audit)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "")
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.filter(outcome="FAILED").count(), 2)

    def test_failed_failure_audit_is_an_explicit_unavailable_error_and_alert(self):
        with patch("cya.core.audit_commands.append_audit", side_effect=IntegrityError("synthetic")):
            with self.assertLogs("cya.core.audit_commands", level="ERROR"):
                with self.assertRaisesRegex(AuditPersistenceFailure, "AUDIT_UNAVAILABLE"):
                    self.command()
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, "")
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_retries_only_rolled_back_deadlocks_and_has_a_three_attempt_budget(self):
        attempts = []

        def prepare(actor):
            def apply(receipt, actor):
                self.assertEqual(User.objects.get(pk=self.user.pk).display_name, "")
                User.objects.filter(pk=self.user.pk).update(display_name="Must roll back")
                attempts.append(1)
                raise OperationalError(1213, "synthetic deadlock")

            return Plan({"saved": True}, apply)

        with patch("cya.core.audit_commands.time.sleep"):
            with self.assertRaises(RetryLater):
                self.command(prepare=prepare)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.get().reason_code, "RETRY_LATER")

    def test_transport_failure_is_not_retried_or_falsely_audited_as_rollback(self):
        def prepare(actor):
            def apply(receipt, actor):
                self.calls.append("attempt")
                raise OperationalError(2013, "synthetic disconnect")

            return Plan({"saved": True}, apply)

        with self.assertRaises(OutcomeUnknown):
            self.command(prepare=prepare)
        self.assertEqual(self.calls, ["attempt"])
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.get().outcome, "OUTCOME_UNKNOWN")

    def test_lost_commit_response_resolves_committed_receipt_without_reexecution(self):
        wrapper = connections["default"]
        real_commit = wrapper.commit
        calls = []

        def commit():
            real_commit()
            calls.append(1)
            if len(calls) == 1:
                raise OperationalError(2013, "synthetic lost commit response")

        with patch.object(wrapper, "commit", side_effect=commit):
            result = self.command()
        self.assertTrue(result.replayed)
        self.assertEqual(self.calls, ["effect"])
        self.assertEqual(CommandReceipt.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.get().outcome, "SUCCEEDED")

    def test_unrelated_integrity_failure_does_not_become_replay(self):
        def fail(receipt, actor):
            raise IntegrityError(1062, "Duplicate entry for key 'unrelated_business_key'")

        with self.assertRaises(IntegrityError):
            self.command(prepare=lambda actor: Plan({"saved": True}, fail))
        self.assertEqual(CommandReceipt.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.get().outcome, "FAILED")

    def test_post_commit_notification_error_does_not_turn_saved_into_failed(self):
        def prepare(actor):
            base = self.prepare(actor)

            def bad_hint():
                raise ValueError("synthetic post-commit notification error")

            def apply(receipt, actor):
                base.apply(receipt, actor)
                transaction.on_commit(bad_hint)

            return Plan(base.response_body, apply)

        with self.assertLogs("cya.core.audit_commands", level="ERROR"):
            result = self.command(prepare=prepare)
        self.assertEqual(result.body, {"saved": True})
        self.assertEqual(CommandReceipt.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.get().outcome, "SUCCEEDED")

    def test_nested_transaction_is_refused_before_any_effect_or_audit(self):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                self.command()
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(self.calls, [])

    def test_failed_worker_keeps_named_service_attribution_without_fabricated_user(self):
        job_key = '["failed-job"]'
        operation = str(
            worker_operation_id(str(self.org.pk), "scheduler", "PROFILE_UPDATE", job_key)
        )

        def prepare(actor):
            def fail(receipt, actor):
                raise ValueError("synthetic")

            return Plan({"saved": True}, fail)

        with self.assertRaises(ValueError):
            self.command(
                operation_id=operation,
                prepare=prepare,
                authorize=lambda org: Actor(service_identity="scheduler", job_key=job_key),
            )
        audit = AuditEvent.objects.get()
        self.assertEqual((audit.service_identity, audit.job_key), ("scheduler", job_key))
        self.assertIsNone(audit.actor_user_id)
        self.assertIsNone(audit.command_id)
        self.assertEqual(audit.roles_at_time, [])
        self.assertEqual(CommandReceipt.objects.count(), 0)

    def test_real_concurrent_worker_duplicate_receipt_arbitration(self):
        barrier = Barrier(2)
        operation = str(
            worker_operation_id(str(self.org.pk), "scheduler", "PROFILE_UPDATE", '["same-job"]')
        )

        def worker(target_id):
            try:

                def authorize(org):
                    return Actor(service_identity="scheduler", job_key='["same-job"]')

                def prepare(actor):
                    # Distinct real resource locks: the unique receipt key, not
                    # a shared resource/actor lock, must arbitrate the duplicate.
                    target = User.objects.select_for_update().get(pk=target_id)
                    barrier.wait(timeout=10)

                    def apply(receipt, actor):
                        User.objects.filter(pk=target.pk).update(display_name="Saved once")

                    return Plan({"target_id": str(target.pk)}, apply)

                return self.command(operation_id=operation, authorize=authorize, prepare=prepare)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, user.pk) for user in (self.user, self.other)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(results[0].body, results[1].body)
        self.assertEqual(sum(result.replayed for result in results), 1)
        self.assertEqual(CommandReceipt.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual(User.objects.filter(display_name="Saved once").count(), 1)

    def test_real_innodb_deadlock_rolls_back_and_retries_whole_command(self):
        barrier, mutex = Barrier(2), Lock()
        attempts = {}

        def worker(actor_id, target_id):
            try:

                def authorize(org):
                    user = User.objects.select_for_update().get(pk=actor_id)
                    with mutex:
                        attempts[actor_id] = attempts.get(actor_id, 0) + 1
                    return Actor(user, ("EMT",))

                def prepare(actor):
                    first = attempts[actor_id] == 1

                    def apply(receipt, actor):
                        if first:
                            barrier.wait(timeout=10)
                        # Deliberately opposite lock order ONLY in this fixture
                        # to provoke a real InnoDB deadlock, not a mocked code.
                        User.objects.filter(pk=target_id).update(display_name="Committed")

                    return Plan({"actor_id": actor_id}, apply)

                return self.command(operation_id=str(uuid4()), authorize=authorize, prepare=prepare)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, self.user.pk, self.other.pk),
                pool.submit(worker, self.other.pk, self.user.pk),
            ]
            results = [f.result(timeout=20) for f in futures]
        self.assertEqual(len(results), 2)
        self.assertGreater(sum(attempts.values()), 2)
        self.assertLessEqual(sum(attempts.values()), 6)
        self.assertEqual(CommandReceipt.objects.count(), 2)
        self.assertEqual(AuditEvent.objects.filter(outcome="SUCCEEDED").count(), 2)
        self.assertEqual(AuditEvent.objects.filter(outcome="FAILED").count(), 0)
