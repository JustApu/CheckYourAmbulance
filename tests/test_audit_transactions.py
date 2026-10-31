"""Offline ordering tests; real transactions are covered in cya/tests/."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from cya.core.audit_transactions import AuditPersistenceFailure, execute_audited_command


@contextmanager
def fake_atomic(*, durable):
    if not durable:
        raise AssertionError("audit transactions must be outermost")
    with patch(
        "cya.core.audit_transactions.transaction.on_commit",
        side_effect=lambda callback, **kwargs: callback(),
    ):
        yield


class AuditTransactionTests(unittest.TestCase):
    @patch("cya.core.audit_transactions.transaction.atomic", side_effect=fake_atomic)
    def test_receipt_before_effects_and_no_failure_audit_on_success(self, atomic):
        calls = []
        result = execute_audited_command(
            reserve_receipt=lambda: calls.append("receipt"),
            write_business_and_audit=lambda: (calls.append("effects"), "ok")[1],
            record_failure_audit=lambda error: calls.append("failure"),
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["receipt", "effects"])
        self.assertEqual(atomic.call_count, 1)

    @patch("cya.core.audit_transactions.transaction.atomic", side_effect=fake_atomic)
    def test_failure_audit_runs_after_business_rollback_boundary(self, atomic):
        calls = []

        def fail():
            calls.append("effects")
            raise ValueError("bad command")

        def record(failure):
            calls.append(("failure", failure.reason_code))

        with self.assertRaisesRegex(ValueError, "bad command"):
            execute_audited_command(
                reserve_receipt=lambda: calls.append("receipt"),
                write_business_and_audit=fail,
                record_failure_audit=record,
            )
        self.assertEqual(calls, ["receipt", "effects", ("failure", "COMMAND_FAILED")])
        self.assertEqual(atomic.call_count, 2)

    @patch("cya.core.audit_transactions.transaction.atomic", side_effect=fake_atomic)
    def test_failed_audit_does_not_mask_failure_as_success(self, atomic):
        def fail():
            raise ValueError("business failure")

        def fail_audit(error):
            raise RuntimeError("database unavailable")

        with self.assertRaises(AuditPersistenceFailure) as caught:
            execute_audited_command(
                reserve_receipt=lambda: None,
                write_business_and_audit=fail,
                record_failure_audit=fail_audit,
            )
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
