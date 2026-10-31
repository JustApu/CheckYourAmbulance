"""Audit append API invariants against MariaDB."""

from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from cya.accounts.models import Organization, User
from cya.core.audit_repository import append_audit, append_receipt
from cya.core.models import AuditEvent, CommandReceipt
from tools.command_identity import worker_operation_id


class AuditRepositoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Audit Test Agency", station_zone="UTC")
        cls.other = Organization.objects.create(name="Other Agency", station_zone="UTC")
        cls.actor = User.objects.create(
            organization=cls.org,
            idp_issuer="https://idp.example.test",
            idp_subject="audit-user",
            email="audit@example.test",
            display_name="Audit User",
        )

    def receipt(self, **overrides):
        kwargs = {
            "organization": self.org,
            "operation_id": str(uuid4()),
            "actor_user": self.actor,
            "command_type": "TEST",
            "request_digest": "a" * 64,
            "response_status": 200,
            "response_body": {"ok": True},
            "correlation_id": str(uuid4()),
        }
        kwargs.update(overrides)
        return append_receipt(**kwargs)

    def test_receipt_and_linked_audit_persist(self):
        receipt = self.receipt()
        audit = append_audit(
            organization=self.org,
            command=receipt,
            request_attempt_id=str(uuid4()),
            action="TEST",
            outcome="SUCCEEDED",
            correlation_id=receipt.correlation_id,
            roles_at_time=["EMT"],
            actor_user=self.actor,
        )
        self.assertEqual(audit.command_id, receipt.pk)

    def test_duplicate_operation_rejected_without_upsert(self):
        receipt = self.receipt()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.receipt(operation_id=receipt.operation_id)
        self.assertEqual(
            CommandReceipt.objects.filter(operation_id=receipt.operation_id).count(), 1
        )

    def test_actor_cross_organization_rejected(self):
        with self.assertRaises(ValidationError):
            self.receipt(organization=self.other)
        self.assertEqual(CommandReceipt.objects.count(), 0)

    def test_worker_operation_requires_v5_and_complete_identity(self):
        worker_id = str(worker_operation_id(str(self.org.pk), "scheduler", "TEST", '["test-job"]'))
        receipt = self.receipt(
            operation_id=worker_id,
            actor_user=None,
            service_identity="scheduler",
            job_key='["test-job"]',
        )
        self.assertEqual(receipt.operation_id, worker_id)
        with self.assertRaises(ValidationError):
            self.receipt(actor_user=None, service_identity="scheduler", job_key=None)

    def test_anonymous_denial_without_receipt(self):
        audit = append_audit(
            organization=self.org,
            request_attempt_id=str(uuid4()),
            action="CHECK",
            outcome="DENIED",
            reason_code="UNAUTHENTICATED",
            correlation_id=str(uuid4()),
            roles_at_time=[],
        )
        self.assertIsNone(audit.command_id)
        self.assertIsNone(audit.actor_user_id)
        self.assertEqual(AuditEvent.objects.count(), 1)

    def test_malformed_payloads_are_rejected_before_insert(self):
        for changes in (
            {"response_body": {"number": float("nan")}},
            {"response_body": {"large": "x" * 262144}},
            {"request_digest": None},
            {"result_schema_version": True},
            {"response_status": 503},
            {"command_type": "raw request body"},
        ):
            with self.subTest(fields=tuple(changes)):
                with self.assertRaises(ValidationError):
                    self.receipt(**changes)
        self.assertEqual(CommandReceipt.objects.count(), 0)
