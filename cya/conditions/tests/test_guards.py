"""Database receipt-attribution insert guards for conditions models (issue 94 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/94))."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from django.db import DatabaseError, transaction
from django.test import TestCase

from cya.accounts.fields import new_id
from cya.accounts.models import Organization, User
from cya.conditions.models import (
    ConditionalRule,
    ObligationDetermination,
    RosterSnapshot,
    SessionRosterCorrection,
)
from cya.core.audit_repository import append_receipt
from tools.command_identity import worker_operation_id


class ConditionsGuardTests(TestCase):
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Sample Agency", station_zone="UTC")
        self.other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        self.admin = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-admin",
            email="admin@example.com",
            display_name="Admin",
        )
        self.supervisor = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-supervisor",
            email="supervisor@example.com",
            display_name="Supervisor",
        )
        self.crew = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-crew",
            email="crew@example.com",
            display_name="Crew",
        )
        self.now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    def refused(self, operation):
        with self.assertRaises(DatabaseError), transaction.atomic():
            operation()

    def human_receipt(self, command_type, actor=None, organization=None):
        org = organization or self.org
        actor_user = actor or self.admin
        with transaction.atomic():
            return append_receipt(
                organization=org,
                operation_id=str(uuid4()),
                actor_user=actor_user,
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={"status": "ok"},
                correlation_id=str(uuid4()),
            )

    def worker_receipt(
        self,
        command_type,
        service_identity="worker.conditions",
        job_key=None,
        organization=None,
    ):
        org = organization or self.org
        key = job_key or json.dumps(["job", str(uuid4())], separators=(",", ":"))
        with transaction.atomic():
            return append_receipt(
                organization=org,
                operation_id=str(
                    worker_operation_id(str(org.pk), service_identity, command_type, key)
                ),
                command_type=command_type,
                request_digest="0" * 64,
                response_status=200,
                response_body={"status": "ok"},
                correlation_id=str(uuid4()),
                service_identity=service_identity,
                job_key=key,
            )

    # -------------------------------------------------------------------------
    # conditional_rule guards
    # -------------------------------------------------------------------------

    def create_rule(self, receipt=None, **overrides):
        receipt = receipt or self.human_receipt("CONDITIONAL_RULE_PUBLISH")
        fields = {
            "organization_id": receipt.organization_id,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": receipt.actor_user,
            "actor_roles_at_time": ["ADMINISTRATOR"],
            "revision": 1,
            "rule_type": "CARRY_OVER_FROM_PRIOR_SHIFT",
            "typed_inputs": {"item_id": new_id()},
            "effective_at": receipt.recorded_at,
        }
        fields.update(overrides)
        return ConditionalRule.objects.create(id=new_id(), **fields)

    def test_conditional_rule_valid_row_is_accepted(self):
        rule = self.create_rule()
        self.assertIsNotNone(rule.pk)

    def test_conditional_rule_mismatched_actor_is_refused(self):
        receipt = self.human_receipt("CONDITIONAL_RULE_PUBLISH", actor=self.admin)
        self.refused(lambda: self.create_rule(receipt=receipt, actor_user=self.crew))

    def test_conditional_rule_mismatched_recorded_at_is_refused(self):
        receipt = self.human_receipt("CONDITIONAL_RULE_PUBLISH")
        past = receipt.recorded_at - timedelta(days=1)
        self.refused(lambda: self.create_rule(receipt=receipt, recorded_at=past))

    def test_conditional_rule_mismatched_organization_is_refused(self):
        receipt = self.human_receipt("CONDITIONAL_RULE_PUBLISH", organization=self.org)
        self.refused(lambda: self.create_rule(receipt=receipt, organization_id=self.other_org.pk))

    def test_conditional_rule_worker_receipt_is_refused(self):
        receipt = self.worker_receipt("CONDITIONAL_RULE_PUBLISH")
        self.refused(lambda: self.create_rule(receipt=receipt, actor_user=self.admin))
        self.refused(lambda: self.create_rule(receipt=receipt, actor_user=None))

    def test_conditional_rule_wrong_command_type_is_refused(self):
        receipt = self.human_receipt("ROLE_CHANGE")
        self.refused(lambda: self.create_rule(receipt=receipt))

    # -------------------------------------------------------------------------
    # roster_snapshot guards
    # -------------------------------------------------------------------------

    def create_snapshot(self, receipt=None, **overrides):
        receipt = receipt or self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        fields = {
            "organization_id": receipt.organization_id,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": receipt.actor_user,
            "actor_roles_at_time": ["ADMINISTRATOR"] if receipt.actor_user else [],
            "service_identity": receipt.service_identity,
            "job_key": receipt.job_key,
            "provider": "synthetic",
            "fetched_at": receipt.recorded_at,
            "availability": "AVAILABLE",
            "shift_start": self.now,
            "shift_end": self.now + timedelta(hours=8),
        }
        fields.update(overrides)
        return RosterSnapshot.objects.create(id=new_id(), **fields)

    def test_roster_snapshot_valid_command_types_are_accepted(self):
        # 1. Worker INGEST_ROSTER_SNAPSHOT
        r1 = self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        snap1 = self.create_snapshot(receipt=r1)
        self.assertIsNotNone(snap1.pk)

        # 2. Worker INGEST_ROSTER_UNAVAILABLE
        r2 = self.worker_receipt("INGEST_ROSTER_UNAVAILABLE")
        snap2 = self.create_snapshot(receipt=r2, availability="UNAVAILABLE")
        self.assertIsNotNone(snap2.pk)

        # 3. Human RECORD_SESSION_ROSTER_CORRECTION
        r3 = self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        snap3 = self.create_snapshot(
            receipt=r3,
            actor_user=self.crew,
            service_identity=None,
            job_key=None,
            provider="SESSION_ACTUAL",
        )
        self.assertIsNotNone(snap3.pk)

    def test_roster_snapshot_mismatched_actor_is_refused(self):
        receipt = self.human_receipt("INGEST_ROSTER_SNAPSHOT", actor=self.admin)
        self.refused(lambda: self.create_snapshot(receipt=receipt, actor_user=self.crew))

    def test_roster_snapshot_mismatched_recorded_at_is_refused(self):
        receipt = self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        past = receipt.recorded_at - timedelta(hours=2)
        self.refused(lambda: self.create_snapshot(receipt=receipt, recorded_at=past))

    def test_roster_snapshot_mismatched_organization_is_refused(self):
        receipt = self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        self.refused(
            lambda: self.create_snapshot(receipt=receipt, organization_id=self.other_org.pk)
        )

    def test_roster_snapshot_worker_human_mismatch_is_refused(self):
        # Human receipt with worker row fields
        hr = self.human_receipt("INGEST_ROSTER_SNAPSHOT", actor=self.admin)
        self.refused(
            lambda: self.create_snapshot(
                receipt=hr,
                actor_user=None,
                service_identity="worker.conditions",
                job_key=json.dumps(["k"]),
            )
        )
        # Worker receipt with human row fields
        wr = self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        self.refused(
            lambda: self.create_snapshot(
                receipt=wr,
                actor_user=self.admin,
                service_identity=None,
                job_key=None,
            )
        )
        # Worker receipt with mismatched job_key
        self.refused(lambda: self.create_snapshot(receipt=wr, job_key="mismatched-key"))
        # Worker receipt with mismatched service_identity
        self.refused(lambda: self.create_snapshot(receipt=wr, service_identity="other.service"))

    def test_roster_snapshot_wrong_command_type_is_refused(self):
        receipt = self.worker_receipt("OBLIGATIONS_RECONCILE")
        self.refused(lambda: self.create_snapshot(receipt=receipt))
        human_rcpt = self.human_receipt("CONDITIONAL_RULE_PUBLISH")
        self.refused(lambda: self.create_snapshot(receipt=human_rcpt))

    # -------------------------------------------------------------------------
    # session_roster_correction guards
    # -------------------------------------------------------------------------

    def create_correction(self, receipt=None, **overrides):
        receipt = receipt or self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        fields = {
            "organization_id": receipt.organization_id,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": receipt.actor_user,
            "actor_roles_at_time": ["CREW"],
            "session_id": new_id(),
            "vehicle_id": new_id(),
            "covers_from": self.now,
            "covers_until": self.now + timedelta(hours=4),
        }
        fields.update(overrides)
        return SessionRosterCorrection.objects.create(id=new_id(), **fields)

    def test_session_roster_correction_valid_row_is_accepted(self):
        corr = self.create_correction()
        self.assertIsNotNone(corr.pk)

    def test_session_roster_correction_mismatched_actor_is_refused(self):
        receipt = self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        self.refused(lambda: self.create_correction(receipt=receipt, actor_user=self.supervisor))

    def test_session_roster_correction_mismatched_recorded_at_is_refused(self):
        receipt = self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        past = receipt.recorded_at - timedelta(minutes=5)
        self.refused(lambda: self.create_correction(receipt=receipt, recorded_at=past))

    def test_session_roster_correction_mismatched_organization_is_refused(self):
        receipt = self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        self.refused(
            lambda: self.create_correction(receipt=receipt, organization_id=self.other_org.pk)
        )

    def test_session_roster_correction_worker_receipt_is_refused(self):
        receipt = self.worker_receipt("RECORD_SESSION_ROSTER_CORRECTION")
        self.refused(lambda: self.create_correction(receipt=receipt, actor_user=self.crew))

    def test_session_roster_correction_wrong_command_type_is_refused(self):
        receipt = self.human_receipt("INGEST_ROSTER_SNAPSHOT", actor=self.crew)
        self.refused(lambda: self.create_correction(receipt=receipt))

    # -------------------------------------------------------------------------
    # obligation_determination guards
    # -------------------------------------------------------------------------

    def create_determination(self, receipt=None, **overrides):
        receipt = receipt or self.human_receipt(
            "RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor
        )
        fields = {
            "organization_id": receipt.organization_id,
            "command": receipt,
            "recorded_at": receipt.recorded_at,
            "actor_user": receipt.actor_user,
            "actor_roles_at_time": ["SUPERVISOR"] if receipt.actor_user else [],
            "service_identity": receipt.service_identity,
            "job_key": receipt.job_key,
            "obligation_id": new_id(),
            "vehicle_id": new_id(),
            "due_at": self.now,
            "determination_type": "WAIVED",
            "evaluated_inputs": {},
        }
        fields.update(overrides)
        return ObligationDetermination.objects.create(id=new_id(), **fields)

    def test_obligation_determination_valid_command_types_are_accepted(self):
        # 1. RECORD_OBLIGATION_DETERMINATION (human)
        r1 = self.human_receipt("RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor)
        d1 = self.create_determination(receipt=r1, determination_type="WAIVED")
        self.assertIsNotNone(d1.pk)

        # 2. RECORD_OBLIGATION_DETERMINATION (worker)
        r2 = self.worker_receipt("RECORD_OBLIGATION_DETERMINATION")
        d2 = self.create_determination(receipt=r2, determination_type="WAIVED")
        self.assertIsNotNone(d2.pk)

        # 3. INGEST_ROSTER_SNAPSHOT (reinstatement during roster ingest)
        r3 = self.worker_receipt("INGEST_ROSTER_SNAPSHOT")
        d3 = self.create_determination(receipt=r3, determination_type="REINSTATED")
        self.assertIsNotNone(d3.pk)

        # 4. INGEST_ROSTER_UNAVAILABLE (reinstatement during roster outage)
        r4 = self.worker_receipt("INGEST_ROSTER_UNAVAILABLE")
        d4 = self.create_determination(receipt=r4, determination_type="REINSTATED")
        self.assertIsNotNone(d4.pk)

        # 5. RECORD_SESSION_ROSTER_CORRECTION (reinstatement during session correction)
        r5 = self.human_receipt("RECORD_SESSION_ROSTER_CORRECTION", actor=self.crew)
        d5 = self.create_determination(receipt=r5, determination_type="REINSTATED")
        self.assertIsNotNone(d5.pk)

        # 6. OBLIGATIONS_RECONCILE (superseded determination in obligation generator)
        r6 = self.worker_receipt("OBLIGATIONS_RECONCILE")
        d6 = self.create_determination(receipt=r6, determination_type="SUPERSEDED")
        self.assertIsNotNone(d6.pk)

    def test_obligation_determination_mismatched_actor_is_refused(self):
        receipt = self.human_receipt("RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor)
        self.refused(lambda: self.create_determination(receipt=receipt, actor_user=self.crew))

    def test_obligation_determination_mismatched_recorded_at_is_refused(self):
        receipt = self.human_receipt("RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor)
        past = receipt.recorded_at - timedelta(hours=1)
        self.refused(lambda: self.create_determination(receipt=receipt, recorded_at=past))

    def test_obligation_determination_mismatched_organization_is_refused(self):
        receipt = self.human_receipt("RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor)
        self.refused(
            lambda: self.create_determination(receipt=receipt, organization_id=self.other_org.pk)
        )

    def test_obligation_determination_worker_human_mismatch_is_refused(self):
        # Human receipt with worker row fields
        hr = self.human_receipt("RECORD_OBLIGATION_DETERMINATION", actor=self.supervisor)
        self.refused(
            lambda: self.create_determination(
                receipt=hr,
                actor_user=None,
                service_identity="worker.conditions",
                job_key=json.dumps(["key"]),
            )
        )
        # Worker receipt with human row fields
        wr = self.worker_receipt("RECORD_OBLIGATION_DETERMINATION")
        self.refused(
            lambda: self.create_determination(
                receipt=wr,
                actor_user=self.supervisor,
                service_identity=None,
                job_key=None,
            )
        )
        # Worker receipt with mismatched job_key
        self.refused(lambda: self.create_determination(receipt=wr, job_key="mismatched-key"))
        # Worker receipt with mismatched service_identity
        self.refused(lambda: self.create_determination(receipt=wr, service_identity="other.worker"))

    def test_obligation_determination_wrong_command_type_is_refused(self):
        receipt = self.human_receipt("ROLE_CHANGE", actor=self.admin)
        self.refused(lambda: self.create_determination(receipt=receipt))
        check_rcpt = self.human_receipt("CHECK_RECORD", actor=self.crew)
        self.refused(lambda: self.create_determination(receipt=check_rcpt))
        cond_publish = self.human_receipt("CONDITIONAL_RULE_PUBLISH", actor=self.admin)
        self.refused(lambda: self.create_determination(receipt=cond_publish))
