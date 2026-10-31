"""Tests for conditional obligations framework and fail-closed roster integration (SPEC 5.6, 15.2, 15.3, AC-5, AC-23)."""

import io
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase, override_settings

from cya.accounts.fields import new_id
from cya.accounts.models import Organization, User
from cya.authorization.commands import change_role
from cya.authorization.roles import grant_initial_crew
from cya.conditions.evaluator import (
    PriorCheckLookupUnavailable,
    RosterStatus,
    check_and_apply_reinstatement,
    derive_effective_roster,
    evaluate_carry_over_rule,
    latest_obligation_determination,
    record_obligation_determination,
)
from cya.conditions.models import (
    ConditionalRule,
    ObligationDetermination,
    RosterMember,
    RosterSnapshot,
    SessionRosterCorrection,
)
from cya.conditions.sync import (
    clear_conflict_notification_sinks,
    create_conditional_rule,
    ingest_roster_snapshot,
    pending_conflict_notifications,
    record_session_roster_correction,
    register_conflict_notification_sink,
    retry_pending_conflict_notifications,
)
from cya.core.audit_commands import CommandDenied
from cya.core.models import AuditEvent
from cya.integrations import RosterShift
from cya.testing.stubs import (
    StubPriorCheckLookup,
    activate_stub_prior_check_lookup,
    fixture_roster_provider,
)


@override_settings(CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup")
class ConditionalObligationsTests(TransactionTestCase):
    def _next_now(self):
        self._clock_now += timedelta(microseconds=100)
        return self._clock_now

    def tearDown(self):
        if hasattr(self, "_clock_patcher"):
            self._clock_patcher.stop()
        clear_conflict_notification_sinks()
        call_command("flush", verbosity=0, interactive=False, reset_sequences=True)
        super().tearDown()

    def setUp(self):
        self.stub_check_lookup = activate_stub_prior_check_lookup()
        self._clock_now = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        self._clock_patcher = patch("django.utils.timezone.now", side_effect=self._next_now)
        self._clock_patcher.start()
        self.org = Organization.objects.create(
            name="sample EMS organization", station_zone="America/New_York"
        )
        self.user_e1 = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-e1",
            email="e1@agency.example",
            display_name="EMT E1",
        )
        self.user_e2 = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-e2",
            email="e2@agency.example",
            display_name="EMT E2",
        )
        self.user_e3 = User.objects.create(
            organization=self.org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-e3",
            email="e3@agency.example",
            display_name="EMT E3",
        )
        with transaction.atomic():
            grant_initial_crew(user=self.user_e1, command_id=str(uuid4()))
            grant_initial_crew(user=self.user_e2, command_id=str(uuid4()))
            grant_initial_crew(user=self.user_e3, command_id=str(uuid4()))
        call_command(
            "bootstrap_administrator",
            organization=self.org.pk,
            user=self.user_e1.pk,
            stdout=io.StringIO(),
        )
        self.vehicle_id = new_id()
        self.item_id = new_id()
        self.rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=1,
            typed_inputs={"item_id": self.item_id},
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            actor_user=self.user_e1,
            actor_roles_at_time=["ADMINISTRATOR"],
        )

    def grant_role(self, user, role):
        change_role(
            organization_id=str(self.org.pk),
            actor_id=str(self.user_e1.pk),
            target_id=str(user.pk),
            operation_id=str(uuid4()),
            correlation_id=str(uuid4()),
            request_attempt_id=str(uuid4()),
            role=role,
            action="GRANT",
        )

    def test_rule_creation_and_immutability(self):
        """Rule revisions are immutable Kind V rows protected by triggers (AC-25, contracts §2)."""
        self.assertEqual(self.rule.revision, 1)
        self.assertEqual(self.rule.rule_type, "CARRY_OVER_FROM_PRIOR_SHIFT")

        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                ConditionalRule.objects.filter(pk=self.rule.pk).update(revision=2)

        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                self.rule.delete()

    def test_roster_snapshot_and_member_immutability(self):
        """Roster snapshots and member rows are append-only Kind A rows."""
        shift = RosterShift(
            shift_id="shift-1",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
        )
        snapshots = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        self.assertEqual(len(snapshots), 1)
        snap = snapshots[0]
        self.assertEqual(snap.members.count(), 2)

        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                RosterSnapshot.objects.filter(pk=snap.pk).update(availability="UNAVAILABLE")

        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                snap.delete()

        member = snap.members.first()
        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                RosterMember.objects.filter(pk=member.pk).update(assignment="DRIVER")

        with transaction.atomic():
            with self.assertRaises(DatabaseError):
                member.delete()

    def test_roster_unavailable_fails_closed(self):
        """When roster data is unavailable, fails closed: obligation is required (CND-3, AC-5)."""
        t = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        result = derive_effective_roster(self.org, self.vehicle_id, t)
        self.assertEqual(result.status, RosterStatus.UNAVAILABLE)
        self.assertFalse(result.is_available)

        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=t,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "c1", "passed": True},
        )
        self.assertFalse(eval_result.waived)
        self.assertIn("fail closed", eval_result.reason.lower())

    def test_unavailable_refresh_invalidates_prior_available_roster(self):
        """A subsequent unavailable refresh invalidates prior available roster (Finding 1, CND-3)."""
        t_shift = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift = RosterShift(
            shift_id="shift-ok",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 18, 0, tzinfo=UTC),
        )
        # 1. Ingest successful feed at 05:00
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            job_key=["feed-ok", "1"],
        )
        res = derive_effective_roster(
            self.org,
            self.vehicle_id,
            t_shift,
            evaluation_time=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
        )
        self.assertEqual(res.status, RosterStatus.SCHEDULED)

        # 2. Ingest failed/unavailable refresh at 05:45
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            fetched_at=datetime(2026, 1, 15, 5, 45, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            is_available=False,
            job_key=["feed-fail", "1"],
        )

        # Derivation at 06:00 must fail closed to UNAVAILABLE, not fallback to old feed!
        res_after = derive_effective_roster(
            self.org,
            self.vehicle_id,
            t_shift,
            evaluation_time=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
        )
        self.assertEqual(res_after.status, RosterStatus.UNAVAILABLE)
        self.assertFalse(res_after.is_available)

    def test_roster_stale_fails_closed(self):
        """When roster is older than staleness limit (3h default), fails closed (HUM-3, CND-3)."""
        fetched = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        target = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)  # 4 hours after fetched
        shift = RosterShift(
            shift_id="shift-2",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=fetched,
            vehicle_id=self.vehicle_id,
        )

        # Checking at evaluation_time = target (4h old feed) -> STALE
        result = derive_effective_roster(self.org, self.vehicle_id, target, evaluation_time=target)
        self.assertEqual(result.status, RosterStatus.STALE)
        self.assertFalse(result.is_available)

    def test_freshness_uses_evaluation_time_not_obligation_time(self):
        """Freshness checks now minus fetched_at rather than obligation boundary minus fetched_at (Finding 2, HUM-3)."""
        fetched = datetime(2026, 1, 15, 5, 30, tzinfo=UTC)
        obligation_due = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)  # D = 06:00
        shift = RosterShift(
            shift_id="shift-fresh",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 18, 0, tzinfo=UTC),
        )
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=fetched,
            vehicle_id=self.vehicle_id,
        )

        # Evaluating at 06:10 (age 40m) -> SCHEDULED
        r_eval_early = derive_effective_roster(
            self.org,
            self.vehicle_id,
            obligation_due,
            evaluation_time=datetime(2026, 1, 15, 6, 10, tzinfo=UTC),
        )
        self.assertEqual(r_eval_early.status, RosterStatus.SCHEDULED)

        # Evaluating at 11:30 (5.5h after D, 6h after fetched_at) -> STALE
        r_eval_late = derive_effective_roster(
            self.org,
            self.vehicle_id,
            obligation_due,
            evaluation_time=datetime(2026, 1, 15, 11, 30, tzinfo=UTC),
        )
        self.assertEqual(r_eval_late.status, RosterStatus.STALE)

    def test_clean_carry_over_shift_waives_obligation(self):
        """When all crew carried over and prior check passed, obligation is waived (AC-5, REQ §2.2)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        # Shift 1: 00:00 - 06:00 with EMT E1, E2, E3
        shift1 = RosterShift("s1", ("e1", "e2", "e3"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        # Shift 2: 06:00 - 12:00 with EMT E1, E2 (subset of prior shift; none begins duty at D)
        shift2 = RosterShift("s2", ("e1", "e2"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-passed-1", "passed": True},
            evaluation_time=d,
        )

        self.assertTrue(eval_result.waived)
        self.assertEqual(eval_result.prior_check_id, "chk-passed-1")
        self.assertIn("carried over from prior shift", eval_result.reason)

        # Record determination
        det = record_obligation_determination(
            organization=self.org,
            obligation_id="ob-1",
            determination_type="WAIVED",
            rule=self.rule,
            explanation=eval_result.reason,
            evaluated_inputs=eval_result.evaluated_inputs,
        )
        self.assertEqual(det.determination_type, "WAIVED")
        self.assertEqual(det.obligation_id, "ob-1")

    def test_crew_change_requires_obligation(self):
        """If any crew member begins duty at D, condition (a) fails and obligation is required (SPEC 5.6)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        # Shift 1 has E1, E2
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        # Shift 2 has E1, E3 (E3 begins duty at D!)
        shift2 = RosterShift("s2", ("e1", "e3"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )

        self.assertFalse(eval_result.waived)
        self.assertIn("began duty at shift boundary", eval_result.reason)
        self.assertFalse(eval_result.evaluated_inputs["condition_a_satisfied"])

    def test_missing_prior_check_requires_obligation(self):
        """If prior shift check did not pass or is missing, condition (b) fails (SPEC 5.6)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Check failed or not found
        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: None,
            evaluation_time=d,
        )

        self.assertFalse(eval_result.waived)
        self.assertIn("no qualifying valid passed check", eval_result.reason)
        self.assertFalse(eval_result.evaluated_inputs["condition_b_satisfied"])

    def test_session_roster_correction_overlays_scheduled_roster(self):
        """Session actual-roster correction overlays scheduled roster for the vehicle (SPEC 15.3, OQ-44)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift = RosterShift("s1", ("e1", "e2"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Session correction updates actual crew on ambulance to E1 + E3
        session_id = new_id()
        record_session_roster_correction(
            organization=self.org,
            session_id=session_id,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        effective = derive_effective_roster(
            self.org, self.vehicle_id, d + timedelta(minutes=10), evaluation_time=d
        )
        self.assertEqual(effective.status, RosterStatus.CORRECTED)
        self.assertEqual(effective.members, frozenset(["e1", "e3"]))

    def test_conflicting_session_roster_corrections_fail_closed_and_notifies(self):
        """Conflicting session corrections fail closed with both recorded and audit notification (AC-23, SPEC 15.3, Finding 3)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        s1 = new_id()
        s2 = new_id()

        record_session_roster_correction(
            organization=self.org,
            session_id=s1,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e2"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        # Concurrent session s2 records a conflicting actual crew
        record_session_roster_correction(
            organization=self.org,
            session_id=s2,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e2,
            actor_roles_at_time=["CREW"],
        )

        effective = derive_effective_roster(
            self.org, self.vehicle_id, d + timedelta(minutes=5), evaluation_time=d
        )
        self.assertEqual(effective.status, RosterStatus.CONFLICT)
        self.assertFalse(effective.is_available)
        self.assertEqual(len(effective.conflicting_corrections), 2)

        # Audit event for conflict notification must exist (Finding 3)
        conflict_audits = AuditEvent.objects.filter(
            action="ROSTER_CONFLICT_DETECTED",
            reason_code="CONCURRENT_SESSION_DISAGREEMENT",
        )
        self.assertTrue(conflict_audits.exists())

    def test_resolves_correction_removes_conflict(self):
        """Supervisor resolution via resolves_correction resolves the conflict (Finding 3, SPEC 15.3)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        s1 = new_id()
        s2 = new_id()

        record_session_roster_correction(
            organization=self.org,
            session_id=s1,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e2"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        corr2 = record_session_roster_correction(
            organization=self.org,
            session_id=s2,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e2,
            actor_roles_at_time=["CREW"],
        )

        # Conflicted
        self.assertEqual(
            derive_effective_roster(
                self.org, self.vehicle_id, d + timedelta(minutes=1), evaluation_time=d
            ).status,
            RosterStatus.CONFLICT,
        )

        # Supervisor resolves by superseding corr2 with a resolution correction
        record_session_roster_correction(
            organization=self.org,
            session_id=s1,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e2"],
            actor_user=self.user_e1,
            actor_roles_at_time=["SUPERVISOR"],
            resolves_correction=corr2,
        )

        # Effective roster now resolves to CORRECTED without conflict
        res = derive_effective_roster(
            self.org, self.vehicle_id, d + timedelta(minutes=2), evaluation_time=d
        )
        self.assertEqual(res.status, RosterStatus.CORRECTED)
        self.assertEqual(res.members, frozenset(["e1", "e2"]))

    def test_correction_automatically_reinstates_waiver(self):
        """A session correction that breaks waiver conditions automatically appends REINSTATED determination (Finding 3)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-passed-1", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_result.waived)

        # Record initial WAIVED determination
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-reinstatement-test",
            determination_type="WAIVED",
            rule=self.rule,
            explanation=eval_result.reason,
            evaluated_inputs=eval_result.evaluated_inputs,
        )

        # Now only invoke the public correction operation with a new crew member e3!
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        # Verify an immutable REINSTATED determination was automatically appended!
        reinstated = ObligationDetermination.objects.filter(
            organization=self.org,
            obligation_id="ob-reinstatement-test",
            determination_type="REINSTATED",
        ).first()
        self.assertIsNotNone(reinstated)
        self.assertIn("Waiver invalidated", reinstated.explanation)
        self.assertIn("e3", reinstated.explanation)

    def test_composite_organization_foreign_keys_reject_cross_org(self):
        """Composite tenant foreign keys reject cross-organization child rows at DB level (Finding 4, contracts §2)."""
        other_org = Organization.objects.create(name="Other Agency", station_zone="UTC")
        other_user = User.objects.create(
            organization=other_org,
            idp_issuer="https://accounts.google.com",
            idp_subject="sub-other",
            email="other@agency.example",
            display_name="Other EMT",
        )

        shift = RosterShift(
            shift_id="tenant-snap-1",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 6, 0, tzinfo=UTC),
        )
        snap = ingest_roster_snapshot(
            organization=self.org,
            provider_name="test",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )[0]

        # 1. RosterMember in other_org referencing snap in self.org fails at DB level
        with transaction.atomic(), self.assertRaises(DatabaseError):
            RosterMember.objects.create(
                id=new_id(),
                organization=other_org,
                snapshot=snap,
                person_identity="intruder",
            )

        # 2. RosterSnapshot with actor_user from other_org fails at DB level (roster_snap_actor_org_fk)
        with transaction.atomic(), self.assertRaises(DatabaseError):
            RosterSnapshot.objects.create(
                id=new_id(),
                organization=self.org,
                command=self.rule.command,
                recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
                actor_user=other_user,
                provider="test",
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                availability="AVAILABLE",
                shift_start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                shift_end=datetime(2026, 1, 1, 6, 0, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

        # 3. ObligationDetermination with actor_user from other_org fails at DB level (ob_det_actor_org_fk)
        with transaction.atomic(), self.assertRaises(DatabaseError):
            ObligationDetermination.objects.create(
                id=new_id(),
                organization=self.org,
                command=self.rule.command,
                recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
                actor_user=other_user,
                obligation_id="ob-tenant-test",
                determination_type="WAIVED",
            )

        # 4. SessionRosterCorrection with actor_user from other_org fails at DB level (sess_corr_actor_org_fk)
        with transaction.atomic(), self.assertRaises(DatabaseError):
            SessionRosterCorrection.objects.create(
                id=new_id(),
                organization=self.org,
                command=self.rule.command,
                recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
                actor_user=other_user,
                session_id=new_id(),
                vehicle_id=self.vehicle_id,
                snapshot=snap,
                covers_from=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                covers_until=datetime(2026, 1, 1, 6, 0, tzinfo=UTC),
            )

        # 5. Model clean() also catches it
        bad_member = RosterMember(
            id=new_id(),
            organization=other_org,
            snapshot=snap,
            person_identity="intruder",
        )
        with self.assertRaises(ValidationError):
            bad_member.clean()

    def test_correction_scope_limits_reinstatement_to_affected_vehicle_and_window(self):
        """A session correction on vehicle A in window W only reinstates waivers for vehicle A within W (Finding 1)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        other_vehicle = new_id()

        # Ingest roster for self.vehicle_id and other_vehicle
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Waiver 1: vehicle A at d (inside correction window)
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-vA-in-window",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Waived carry over",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC).isoformat(),
                "prior_shift_end": d.isoformat(),
                "prior_check_id": "chk-1",
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(self.vehicle_id),
                "due_at": d.isoformat(),
            },
        )

        # Waiver 2: vehicle B at d (inside window, but different vehicle)
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-vB-in-window",
            determination_type="WAIVED",
            vehicle_id=other_vehicle,
            due_at=d,
            rule=self.rule,
            explanation="Waived carry over other vehicle",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC).isoformat(),
                "prior_shift_end": d.isoformat(),
                "prior_check_id": "chk-1",
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(other_vehicle),
                "due_at": d.isoformat(),
            },
        )

        # Waiver 3: vehicle A at d + 48h (same vehicle, but 2 days outside window)
        d_future = d + timedelta(days=2)
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-vA-future",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d_future,
            rule=self.rule,
            explanation="Waived carry over future",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": (d_future - timedelta(hours=6)).isoformat(),
                "prior_shift_end": d_future.isoformat(),
                "prior_check_id": "chk-1",
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(self.vehicle_id),
                "due_at": d_future.isoformat(),
            },
        )

        # Record correction on vehicle A covering [d, d + 6h] with new crew member e3
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        # Obligation 1 on vehicle A inside window MUST be reinstated
        self.assertTrue(
            ObligationDetermination.objects.filter(
                obligation_id="ob-vA-in-window", determination_type="REINSTATED"
            ).exists()
        )

        # Obligation 2 on vehicle B MUST NOT be reinstated
        self.assertFalse(
            ObligationDetermination.objects.filter(
                obligation_id="ob-vB-in-window", determination_type="REINSTATED"
            ).exists()
        )

        # Obligation 3 on vehicle A in the future (outside window) MUST NOT be reinstated
        self.assertFalse(
            ObligationDetermination.objects.filter(
                obligation_id="ob-vA-future", determination_type="REINSTATED"
            ).exists()
        )

    def test_correction_does_not_reinstate_an_already_reinstated_waiver(self):
        """A correction does not reinstate an obligation whose latest determination is already REINSTATED."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        ob_id = "ob-supervisor-completed"

        # Record WAIVED determination
        waiver = record_obligation_determination(
            organization=self.org,
            obligation_id=ob_id,
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Waived carry over",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "vehicle_id": str(self.vehicle_id),
                "due_at": d.isoformat(),
            },
        )

        # Reinstatement has already superseded the waiver, so another correction is a no-op.
        record_obligation_determination(
            organization=self.org,
            obligation_id=ob_id,
            determination_type="REINSTATED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            prior_determination=waiver,
            explanation="Waiver reinstated by synthetic roster correction.",
            evaluated_inputs={
                "prior_determination_id": str(waiver.id),
                "vehicle_id": self.vehicle_id,
            },
        )

        # Record correction on vehicle A with new crew
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e1,
            actor_roles_at_time=["CREW"],
        )

        # The obligation's latest determination remains REINSTATED, without a duplicate row.
        latest = (
            ObligationDetermination.objects.filter(obligation_id=ob_id)
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")
        self.assertEqual(
            ObligationDetermination.objects.filter(
                obligation_id=ob_id, determination_type="REINSTATED"
            ).count(),
            1,
        )

    def test_hourly_refresh_records_new_snapshot_when_crew_changes(self):
        """Distinct hourly fetches of the same shift record new snapshots with updated fetched_at and members (Finding 2)."""
        start = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        end = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)

        shift_at_8 = RosterShift("shift-100", ("e1",), start, end)
        snaps1 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_at_8],
            fetched_at=datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        self.assertEqual(len(snaps1), 1)

        # At 09:00, humanity feed refreshed with new crew member e2
        shift_at_9 = RosterShift("shift-100", ("e1", "e2"), start, end)
        snaps2 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_at_9],
            fetched_at=datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        self.assertEqual(len(snaps2), 1)
        self.assertNotEqual(snaps1[0].id, snaps2[0].id)

        # Derivation at 09:30 uses the refreshed snapshot (not stale, contains e1 and e2)
        effective = derive_effective_roster(
            self.org,
            self.vehicle_id,
            datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
            evaluation_time=datetime(2026, 1, 15, 9, 30, tzinfo=UTC),
        )
        self.assertEqual(effective.status, RosterStatus.SCHEDULED)
        self.assertEqual(effective.members, frozenset(["e1", "e2"]))

        # Retry with identical 09:00 inputs safely replays snaps2
        snaps_retry = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_at_9],
            fetched_at=datetime(2026, 1, 15, 9, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        self.assertEqual(snaps_retry[0].id, snaps2[0].id)

    def test_authoritative_empty_refresh_fails_closed_and_prevents_waiver(self):
        """A successful empty refresh records an empty AVAILABLE snapshot, preventing stale crew from surviving (Finding 3)."""
        start = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        end = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)

        # Initial feed with crew e1, e2
        shift1 = RosterShift("shift-200", ("e1", "e2"), start, end)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1],
            fetched_at=datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Later feed at 10:00 returns empty shifts for this window (e.g. shift was deleted/cancelled)
        empty_snaps = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[],
            fetched_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=start,
            interval_end=end,
        )
        self.assertEqual(len(empty_snaps), 1)
        self.assertEqual(empty_snaps[0].availability, "AVAILABLE")
        self.assertEqual(empty_snaps[0].members.count(), 0)

        # Derivation at 10:30 reflects empty crew
        effective = derive_effective_roster(
            self.org,
            self.vehicle_id,
            datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
            evaluation_time=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        )
        self.assertEqual(effective.status, RosterStatus.SCHEDULED)
        self.assertEqual(effective.members, frozenset())

        # Waiver evaluation at 10:30 fails condition (a) (no crew on duty)
        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-passed-1", "passed": True},
            evaluation_time=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
        )
        self.assertFalse(eval_result.waived)
        self.assertIn("zero members on duty", eval_result.reason)

    def test_command_authorization_enforces_active_user_and_roles(self):
        """Command authorization loads current DB roles and enforces permissions at boundaries (Finding 4)."""
        # 1. Inactive administrator cannot publish conditional rule
        self.user_e1.active = False
        self.user_e1.save(update_fields=["active"])

        with self.assertRaises(CommandDenied):
            create_conditional_rule(
                organization=self.org,
                rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                revision=2,
                typed_inputs={"item_id": self.item_id},
                effective_at=datetime(2026, 2, 1, tzinfo=UTC),
                actor_user=self.user_e1,
            )

        self.user_e1.active = True
        self.user_e1.save(update_fields=["active"])

        # 2. Crew user (user_e2) cannot publish conditional rule
        with self.assertRaises(CommandDenied):
            create_conditional_rule(
                organization=self.org,
                rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                revision=2,
                typed_inputs={"item_id": self.item_id},
                effective_at=datetime(2026, 2, 1, tzinfo=UTC),
                actor_user=self.user_e2,
            )

        # 3. Crew user (user_e2) cannot resolve conflicting session
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        corr_to_resolve = record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e2"],
            actor_user=self.user_e2,
        )

        with self.assertRaises(CommandDenied):
            record_session_roster_correction(
                organization=self.org,
                session_id=new_id(),
                vehicle_id=self.vehicle_id,
                covers_from=d,
                covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                members=["e1", "e2"],
                actor_user=self.user_e2,
                resolves_correction=corr_to_resolve,
            )

        # 4. Supervisor or Administrator CAN resolve
        self.grant_role(self.user_e3, "SUPERVISOR")
        resolved = record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            members=["e1", "e3"],
            actor_user=self.user_e3,
            resolves_correction=corr_to_resolve,
        )
        self.assertEqual(resolved.resolves_correction_id, corr_to_resolve.id)

    def test_conflict_records_notification_intent_and_both_provenances(self):
        """Conflict delivers notification intent to supervisors and preserves both conflict provenances in reinstatement (Finding 6)."""
        captured_notifications = []
        register_conflict_notification_sink(lambda intent: captured_notifications.append(intent))

        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)

        # Ingest initial shift and WAIVED determination
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-conflict-provenance",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Initial waiver",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC).isoformat(),
                "prior_shift_end": d.isoformat(),
                "prior_check_id": "chk-1",
                "prior_check_checked_at": datetime(2026, 1, 15, 0, 30, tzinfo=UTC).isoformat(),
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(self.vehicle_id),
                "due_at": d.isoformat(),
            },
        )

        s1 = new_id()
        s2 = new_id()

        def check_lookup(*args, obligation_id=None):
            if obligation_id is not None:
                return None
            return {
                "id": "chk-1",
                "passed": True,
                "checked_at": datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            }

        with override_settings(CYA_PRIOR_CHECK_LOOKUP=check_lookup):
            corr1 = record_session_roster_correction(
                organization=self.org,
                session_id=s1,
                vehicle_id=self.vehicle_id,
                covers_from=d,
                covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                members=["e1", "e2"],
                actor_user=self.user_e2,
            )

            # Second conflicting session
            corr2 = record_session_roster_correction(
                organization=self.org,
                session_id=s2,
                vehicle_id=self.vehicle_id,
                covers_from=d,
                covers_until=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                members=["e1", "e3"],
                actor_user=self.user_e3,
            )

        # Verify supervisor notification intent was generated
        self.assertEqual(len(captured_notifications), 1)
        intent = captured_notifications[0]
        self.assertEqual(intent["vehicle_id"], str(self.vehicle_id))
        self.assertIn("SUPERVISOR", intent["target_roles"])
        self.assertIn(str(corr1.id), intent["conflicting_correction_ids"])
        self.assertIn(str(corr2.id), intent["conflicting_correction_ids"])
        self.assertIn(str(s1), intent["conflicting_sessions"])
        self.assertIn(str(s2), intent["conflicting_sessions"])

        # Verify reinstatement contains both conflicting correction references
        reinstated = ObligationDetermination.objects.filter(
            obligation_id="ob-conflict-provenance",
            determination_type="REINSTATED",
        ).first()
        self.assertIsNotNone(reinstated)
        inputs = reinstated.evaluated_inputs
        self.assertIn(str(corr1.id), inputs["conflicting_correction_ids"])
        self.assertIn(str(corr2.id), inputs["conflicting_correction_ids"])

    def test_fleet_wide_roster_fallback_with_null_vehicle_id(self):
        """Fleet-wide roster snapshot with vehicle_id=None matches vehicle queries (Finding 5)."""
        t = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift = RosterShift(
            shift_id="shift-fleet",
            user_ids=("e1", "e2"),
            start=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        # Ingest with default vehicle_id=None
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=None,
        )

        # Derivation for a specific vehicle should find the fleet-wide snapshot
        effective = derive_effective_roster(
            self.org, self.vehicle_id, t + timedelta(minutes=1), evaluation_time=t
        )
        self.assertEqual(effective.status, RosterStatus.SCHEDULED)
        self.assertEqual(effective.members, frozenset(["e1", "e2"]))

    def test_command_replay_and_idempotency(self):
        """Commands support durable replay on retry with same operation/job identity (Finding 6)."""
        op_id = str(uuid4())
        # Rule creation replay
        rule1 = create_conditional_rule(
            organization=self.org,
            rule_type="SEAL_SUBSTITUTION",
            typed_inputs={"seal": "tamper-evident"},
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            actor_user=self.user_e1,
            actor_roles_at_time=["ADMINISTRATOR"],
            operation_id=op_id,
        )
        rule2 = create_conditional_rule(
            organization=self.org,
            rule_type="SEAL_SUBSTITUTION",
            typed_inputs={"seal": "tamper-evident"},
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            actor_user=self.user_e1,
            actor_roles_at_time=["ADMINISTRATOR"],
            operation_id=op_id,
        )
        self.assertEqual(rule1.pk, rule2.pk)

        # Ingest snapshot replay with stable job key
        shift = RosterShift(
            shift_id="shift-replay",
            user_ids=("e1",),
            start=datetime(2026, 1, 15, 6, 0, tzinfo=UTC),
            end=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        snaps1 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            job_key=["replay-key", "shift-replay"],
        )
        snaps2 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            job_key=["replay-key", "shift-replay"],
        )
        self.assertEqual(snaps1[0].pk, snaps2[0].pk)
        self.assertEqual(
            RosterSnapshot.objects.filter(provider="humanity", shift_start=shift.start).count(), 1
        )

    def test_fixture_roster_provider_integration(self):
        """Ensure synthetic base.json fixture integrates smoothly with ingest (AC-5, AC-28)."""
        provider = fixture_roster_provider()
        shifts = provider.shifts_overlapping(
            datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 23, 59, tzinfo=UTC),
        )
        self.assertGreater(len(shifts), 0)

        snapshots = ingest_roster_snapshot(
            organization=self.org,
            provider_name="fixture_stub",
            shifts=shifts,
            fetched_at=datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        self.assertEqual(len(snapshots), len(shifts))

    def test_scheduled_refresh_automatically_reinstates_waiver(self):
        """Scheduled refreshes (crew change, UNAVAILABLE, or empty shifts=[]) automatically reinstate waivers (Finding 1)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        shift1 = RosterShift("s1", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC))

        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Seed 3 waived obligations at d on different vehicles/obligations to test all 3 refresh modes
        base_inputs = {
            "prior_members": ["e1", "e2"],
            "prior_shift_start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC).isoformat(),
            "prior_shift_end": d.isoformat(),
            "prior_check_id": "chk-1",
            "condition_b_satisfied": True,
            "item_id": str(self.item_id),
            "vehicle_id": str(self.vehicle_id),
            "due_at": d.isoformat(),
        }
        for ob_id in ("ob-sched-crew", "ob-sched-unavail", "ob-sched-empty"):
            record_obligation_determination(
                organization=self.org,
                obligation_id=ob_id,
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation="Initial carry-over waiver",
                evaluated_inputs=dict(base_inputs),
            )

        # (a) Scheduled refresh changes crew at d to ("e1", "e3") -> automatically reinstates!
        shift2_changed = RosterShift(
            "s2", ("e1", "e3"), d, datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        )
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift2_changed],
            fetched_at=datetime(2026, 1, 15, 5, 15, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        reinstated_crew = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-sched-crew",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(reinstated_crew.determination_type, "REINSTATED")
        self.assertIn("e3", reinstated_crew.explanation)

        # Re-waive ob-sched-unavail and ob-sched-empty after restoring carry-over crew
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift2],
            fetched_at=datetime(2026, 1, 15, 5, 20, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-sched-unavail",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Re-waived before unavailable refresh",
            evaluated_inputs=dict(base_inputs),
            job_key=["rewaive-unavail"],
        )

        # (b) Scheduled refresh marks provider UNAVAILABLE -> automatically reinstates!
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        reinstated_unavail = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-sched-unavail",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(reinstated_unavail.determination_type, "REINSTATED")
        self.assertIn("UNAVAILABLE", reinstated_unavail.explanation)

        # Restore available shift and re-waive ob-sched-empty
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 40, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-sched-empty",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Re-waived before empty refresh",
            evaluated_inputs=dict(base_inputs),
            job_key=["rewaive-empty"],
        )

        # (c) Scheduled refresh ingests authoritative empty window (shifts=[]) -> automatically reinstates!
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[],
            fetched_at=datetime(2026, 1, 15, 5, 50, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            interval_end=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        reinstated_empty = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-sched-empty",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(reinstated_empty.determination_type, "REINSTATED")
        self.assertIn("No scheduled or actual crew", reinstated_empty.explanation)

    def test_fleet_wide_empty_refresh_supersedes_vehicle_specific_data(self):
        """A newer fleet-wide empty refresh supersedes older vehicle-specific shifts and reinstates waivers (Finding 2)."""
        d = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        end = datetime(2026, 1, 15, 16, 0, tzinfo=UTC)

        # 1. Vehicle-specific shift at 08:00 (and preceding shift 00:00-08:00)
        shift_prior = RosterShift(
            "shift-veh-prior", ("e1", "e2"), datetime(2026, 1, 15, 0, 0, tzinfo=UTC), d
        )
        shift_veh = RosterShift("shift-veh-1", ("e1", "e2"), d, end)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_prior, shift_veh],
            fetched_at=datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-fleet-empty-supersede",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Waived on vehicle-specific shift",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC).isoformat(),
                "prior_shift_end": d.isoformat(),
                "prior_check_id": "chk-1",
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(self.vehicle_id),
                "due_at": d.isoformat(),
            },
        )

        # 2. Later fleet-wide empty refresh (vehicle_id=None, shifts=[]) at 07:30
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[],
            fetched_at=datetime(2026, 1, 15, 7, 30, tzinfo=UTC),
            vehicle_id=None,
            interval_start=d,
            interval_end=end,
        )

        # Effective roster for self.vehicle_id must reflect the newer empty feed, NOT older vehicle-specific crew!
        effective = derive_effective_roster(
            self.org,
            self.vehicle_id,
            d + timedelta(minutes=30),
            evaluation_time=datetime(2026, 1, 15, 8, 30, tzinfo=UTC),
        )
        self.assertEqual(effective.status, RosterStatus.SCHEDULED)
        self.assertEqual(effective.members, frozenset())

        # And the waiver on self.vehicle_id must have been automatically reinstated!
        latest = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-fleet-empty-supersede",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")

    def test_carry_over_requires_preceding_shift_ending_at_due_boundary(self):
        """A single continuous 00:00-12:00 shift evaluated at D=06:00 fails closed without a preceding shift ending at D (Finding 3)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        continuous_shift = RosterShift(
            "shift-continuous",
            ("e1", "e2"),
            datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[continuous_shift],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_result = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertFalse(eval_result.waived)
        self.assertIn("no preceding shift ending at obligation boundary", eval_result.reason)
        self.assertTrue(eval_result.evaluated_inputs["fail_closed"])

    def test_conflict_notification_intent_is_durable_and_records_sink_failures(self):
        """Conflict notification intent persists durably even without sinks or on sink failure, and supports retry (Finding 4)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        end = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        # Register a failing sink
        def failing_sink(intent):
            raise RuntimeError("Simulated notification channel outage")

        register_conflict_notification_sink(failing_sink)

        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=end,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=end,
            members=["e1", "e3"],
            actor_user=self.user_e2,
        )

        # 1. Sink failure is durably recorded in AuditEvent (not silently lost)
        failed_events = AuditEvent.objects.filter(
            organization=self.org,
            action="ROSTER_CONFLICT_NOTIFICATION_FAILED",
            outcome="FAILED",
            reason_code="NOTIFICATION_SINK_ERROR",
        )
        self.assertEqual(failed_events.count(), 1)
        failed_vals = failed_events.first().new_snapshot["values"]
        self.assertEqual(failed_vals["delivery_status"], "FAILED")
        self.assertTrue(any("RuntimeError" in err for err in failed_vals["delivery_errors"]))

        # 2. Notification remains pending/retryable in durable storage
        pending = pending_conflict_notifications(self.org)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["vehicle_id"], str(self.vehicle_id))

        # 3. Replace failing sink with working sink and retry pending notifications
        clear_conflict_notification_sinks()
        delivered = []
        register_conflict_notification_sink(lambda intent: delivered.append(intent))

        retried = retry_pending_conflict_notifications(self.org)
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["delivery_status"], "DELIVERED")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(pending_conflict_notifications(self.org)), 0)

    def test_partial_window_overlap_conflict_detected_and_provenance_recorded(self):
        """Conflict is detected even when disagreement occurs inside [covers_from, covers_until] rather than at covers_from (Finding 5)."""
        t06 = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t08 = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t09 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        captured_notifications = []
        register_conflict_notification_sink(lambda intent: captured_notifications.append(intent))

        # Seed a WAIVED determination at 09:00 (inside the overlap [08:00, 12:00]) with preceding shift [03:00, 09:00)
        t03 = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
        shift_prior = RosterShift("s-prior-09", ("e1", "e2"), t03, t09)
        shift_curr = RosterShift("s-curr-09", ("e1", "e2"), t09, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_prior, shift_curr],
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-overlap-0900",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=t09,
            rule=self.rule,
            explanation="Initial waiver at 09:00",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "prior_shift_start": t03.isoformat(),
                "prior_shift_end": t09.isoformat(),
                "prior_check_id": "chk-1",
                "prior_check_checked_at": datetime(2026, 1, 15, 4, 0, tzinfo=UTC).isoformat(),
                "condition_b_satisfied": True,
                "item_id": str(self.item_id),
                "vehicle_id": str(self.vehicle_id),
                "due_at": t09.isoformat(),
            },
        )

        def check_lookup(*args, obligation_id=None):
            if obligation_id is not None:
                return None
            return {
                "id": "chk-1",
                "passed": True,
                "checked_at": datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
            }

        with override_settings(CYA_PRIOR_CHECK_LOOKUP=check_lookup):
            # 1. Existing correction covers 08:00-12:00
            s1 = new_id()
            corr1 = record_session_roster_correction(
                organization=self.org,
                session_id=s1,
                vehicle_id=self.vehicle_id,
                covers_from=t08,
                covers_until=t12,
                members=["e1", "e2"],
                actor_user=self.user_e1,
            )

            # 2. New conflicting correction covers 06:00-12:00 (no disagreement at covers_from=06:00, conflict on 08:00-12:00!)
            s2 = new_id()
            corr2 = record_session_roster_correction(
                organization=self.org,
                session_id=s2,
                vehicle_id=self.vehicle_id,
                covers_from=t06,
                covers_until=t12,
                members=["e1", "e3"],
                actor_user=self.user_e2,
            )

        # Verify ROSTER_CONFLICT_DETECTED and supervisor notification were triggered
        self.assertEqual(len(captured_notifications), 1)
        self.assertIn(str(corr1.id), captured_notifications[0]["conflicting_correction_ids"])
        self.assertIn(str(corr2.id), captured_notifications[0]["conflicting_correction_ids"])

        # Verify reinstated determination at 09:00 records both conflicting correction IDs and session IDs
        reinstated = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-overlap-0900",
                determination_type="REINSTATED",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertIsNotNone(reinstated)
        self.assertIn(str(corr1.id), reinstated.evaluated_inputs["conflicting_correction_ids"])
        self.assertIn(str(corr2.id), reinstated.evaluated_inputs["conflicting_correction_ids"])
        self.assertIn(str(s1), reinstated.evaluated_inputs["conflicting_sessions"])
        self.assertIn(str(s2), reinstated.evaluated_inputs["conflicting_sessions"])

    def test_nonempty_refresh_dropping_shift_supersedes_and_reinstates_waiver(self):
        """A non-empty authoritative refresh that drops one of the shifts in the polled interval removes that shift (Codex Finding 1)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_res.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-dropped-shift",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_res.reason,
            evaluated_inputs=eval_res.evaluated_inputs,
        )

        # Later authoritative refresh for [t00, t12) returns ONLY shift1 (shift2 was deleted!)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1],
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        # Effective roster at d must be empty (0 members), NOT the stale shift2!
        eff_at_d = derive_effective_roster(self.org, self.vehicle_id, d, evaluation_time=d)
        self.assertEqual(eff_at_d.status, RosterStatus.SCHEDULED)
        self.assertEqual(eff_at_d.members, frozenset())

        # And the waiver at d must have been automatically reinstated!
        latest = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-dropped-shift",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")

    def test_independent_unavailable_outages_record_distinct_snapshots_and_reinstate(self):
        """Outage -> recovery -> second outage at a later fetched_at records a new UNAVAILABLE snapshot and reinstates (Codex Finding 2)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        # 1. First outage for [t00, t12) at 04:00
        outage1 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        # Retry with identical fetched_at replays outage1 idempotently
        outage1_retry = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 4, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )
        self.assertEqual(outage1[0].id, outage1_retry[0].id)

        # 2. Recovery at 05:00 with valid carry-over shifts
        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_res.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-second-outage",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_res.reason,
            evaluated_inputs=eval_res.evaluated_inputs,
        )

        # 3. Second outage for the same [t00, t12) window at 05:30
        outage2 = ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )
        self.assertNotEqual(outage1[0].id, outage2[0].id)

        eff_after_outage2 = derive_effective_roster(self.org, self.vehicle_id, d, evaluation_time=d)
        self.assertEqual(eff_after_outage2.status, RosterStatus.UNAVAILABLE)

        latest = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-second-outage",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")

    def test_continuous_shift_replacement_reinstates_waiver_via_carry_over_rule(self):
        """Replacing 00:00-06:00 and 06:00-12:00 shifts with one continuous 00:00-12:00 shift reinstates the 06:00 waiver (Codex Finding 3)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_res.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-merged-shift",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_res.reason,
            evaluated_inputs=eval_res.evaluated_inputs,
        )

        # Later refresh replaces the two shifts with a single continuous 00:00-12:00 shift
        merged_shift = RosterShift("s-merged", ("e1", "e2"), t00, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[merged_shift],
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        latest = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-merged-shift",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")
        self.assertIn("no preceding shift ending at obligation boundary", latest.explanation)

    def test_prior_shift_only_change_selects_and_reinstates_waiver(self):
        """A correction or refresh confined strictly to the preceding shift [00:00, D) invalidates and reinstates a waiver at D (Codex Finding 4)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_res.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-prior-shift-change",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_res.reason,
            evaluated_inputs=eval_res.evaluated_inputs,
        )

        # Record a session correction ONLY for the prior shift [t00, d) showing only ["e1"] worked (e2 was not on prior shift!)
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=t00,
            covers_until=d,
            members=["e1"],
            actor_user=self.user_e1,
        )

        latest = (
            ObligationDetermination.objects.filter(
                organization=self.org,
                obligation_id="ob-prior-shift-change",
            )
            .order_by("-recorded_at")
            .first()
        )
        self.assertEqual(latest.determination_type, "REINSTATED")
        self.assertIn("e2", latest.explanation)

    def test_delayed_same_crew_refresh_uses_server_evaluation_clock_to_detect_staleness(self):
        """Under CND-7, a delayed unchanged refresh (fetched_at = D + 1m at server clock D + 5h) preserves an existing waiver, while new determinations fail closed as STALE (HUM-3, CND-3, CND-7)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        def check_lookup(*args, obligation_id=None):
            if obligation_id is not None:
                return None
            return {
                "id": "chk-1",
                "passed": True,
                "checked_at": datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            }

        with override_settings(CYA_PRIOR_CHECK_LOOKUP=check_lookup):
            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-delayed-stale",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # Ingest a delayed unchanged snapshot whose fetched_at is d + 1m, when server clock is d + 5h (> 3h staleness limit)
            server_now = d + timedelta(hours=5)
            with patch("django.utils.timezone.now", return_value=server_now):
                ingest_roster_snapshot(
                    organization=self.org,
                    provider_name="humanity",
                    shifts=[shift1, shift2],
                    fetched_at=d + timedelta(minutes=1),
                    vehicle_id=self.vehicle_id,
                )

            # Existing waiver granted on fresh data remains WAIVED under CND-7 (no REINSTATED from aged unchanged data)
            latest = latest_obligation_determination(self.org, "ob-delayed-stale")
            self.assertEqual(latest.determination_type, "WAIVED")

            # A new determination evaluated at server_now (d + 5h) still fails closed as STALE under HUM-3 / CND-3
            new_eval = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=server_now,
            )
            self.assertFalse(new_eval.waived)
            self.assertIn("stale", new_eval.reason.lower())

    def test_half_open_correction_interval_does_not_bleed_into_next_shift(self):
        """A correction for [00:00, 06:00) does not apply at D=06:00 or conflict with [06:00, 12:00) (Claude Finding 1)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        # Scheduled shifts: prior [00:00, 06:00) has e1, current [06:00, 12:00) has e1, e2, e3
        shift1 = RosterShift("s1", ("e1",), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2", "e3"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Prior shift correction [00:00, 06:00) adds e2 -> prior crew is {e1, e2}
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=t00,
            covers_until=d,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )

        # At d=06:00, current roster must be the scheduled shift2 {e1, e2, e3}, NOT the prior correction {e1, e2}!
        eff_at_d = derive_effective_roster(self.org, self.vehicle_id, d, evaluation_time=d)
        self.assertEqual(eff_at_d.status, RosterStatus.SCHEDULED)
        self.assertEqual(eff_at_d.members, frozenset(["e1", "e2", "e3"]))

        # And carry-over rule at d must NOT waive because e3 began duty at d!
        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertFalse(eval_res.waived)
        self.assertIn("e3", eval_res.reason)

        # Recording an adjacent correction for [06:00, 12:00) must NOT conflict with [00:00, 06:00) at 06:00
        captured_notifications = []
        register_conflict_notification_sink(lambda intent: captured_notifications.append(intent))
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e3"],
            actor_user=self.user_e2,
        )
        self.assertEqual(len(captured_notifications), 0)
        eff_at_d_after = derive_effective_roster(self.org, self.vehicle_id, d, evaluation_time=d)
        self.assertEqual(eff_at_d_after.status, RosterStatus.CORRECTED)
        self.assertEqual(eff_at_d_after.members, frozenset(["e1", "e3"]))

    def test_determination_and_rule_authorization_boundaries(self):
        """Human users cannot record WAIVED/REINSTATED, CREW cannot record SUPERVISOR_COMPLETED, and anonymous rule publish is denied (Claude Finding 2)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)

        # 1. Anonymous actor_user=None cannot publish a conditional rule
        with self.assertRaises(CommandDenied):
            create_conditional_rule(
                organization=self.org,
                rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                typed_inputs={"item_id": self.item_id},
                effective_at=datetime(2026, 3, 1, tzinfo=UTC),
                actor_user=None,
            )

        # 2. Even an Administrator human user cannot directly record WAIVED or REINSTATED (must be system evaluator)
        with self.assertRaises(CommandDenied):
            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-auth-1",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                actor_user=self.user_e1,
            )

        # 3. CREW user (user_e2) cannot record SUPERVISOR_COMPLETED
        with self.assertRaises(CommandDenied):
            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-auth-2",
                determination_type="SUPERVISOR_COMPLETED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                actor_user=self.user_e2,
            )

        # 4. System actor (actor_user=None) cannot record SUPERVISOR_COMPLETED
        with self.assertRaises(CommandDenied):
            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-auth-3",
                determination_type="SUPERVISOR_COMPLETED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                actor_user=None,
            )

    def test_conditional_rule_validation_and_evaluator_rule_checks(self):
        """create_conditional_rule validates typed_inputs and evaluate_carry_over_rule fails closed on wrong/future/other-org rules (Claude Finding 3)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        # 1. Invalid typed_inputs rejected at creation time
        with self.assertRaises(ValidationError):
            create_conditional_rule(
                organization=self.org,
                rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                typed_inputs={},
                effective_at=datetime(2026, 3, 1, tzinfo=UTC),
                actor_user=self.user_e1,
            )
        with self.assertRaises(ValidationError):
            create_conditional_rule(
                organization=self.org,
                rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
                typed_inputs={"unknown_key": "val"},
                effective_at=datetime(2026, 3, 1, tzinfo=UTC),
                actor_user=self.user_e1,
            )

        # Setup valid shifts that would otherwise waive
        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # 2. SEAL_SUBSTITUTION rule passed to evaluate_carry_over_rule fails closed
        seal_rule = create_conditional_rule(
            organization=self.org,
            rule_type="SEAL_SUBSTITUTION",
            typed_inputs={"seal": "seal-1"},
            effective_at=datetime(2026, 1, 1, tzinfo=UTC),
            actor_user=self.user_e1,
        )
        res_seal = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=seal_rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertFalse(res_seal.waived)
        self.assertTrue(res_seal.evaluated_inputs["fail_closed"])

        # 3. Future rule (effective_at > due_at) fails closed
        future_rule = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            typed_inputs={"item_id": self.item_id},
            effective_at=datetime(2026, 2, 1, tzinfo=UTC),
            actor_user=self.user_e1,
        )
        res_future = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            rule=future_rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertFalse(res_future.waived)
        self.assertTrue(res_future.evaluated_inputs["fail_closed"])

        # 4. Mismatched item_id fails closed
        res_wrong_item = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id="different-item-id",
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d,
        )
        self.assertFalse(res_wrong_item.waived)
        self.assertIn("item_id does not match", res_wrong_item.reason)

    def test_resolving_session_latest_correction_does_not_revive_earlier_correction(self):
        """When a session records c1_old then c1_new, resolving c1_new does not revive c1_old (Claude Finding 4)."""
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        s1 = new_id()
        s2 = new_id()

        # Session s1 records initial correction ["e1", "e99"] then updates to ["e1", "e3"]
        record_session_roster_correction(
            organization=self.org,
            session_id=s1,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e99"],
            actor_user=self.user_e1,
        )
        c1_new = record_session_roster_correction(
            organization=self.org,
            session_id=s1,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e3"],
            actor_user=self.user_e1,
        )

        # Session s2 records ["e1", "e2"], conflicting with s1's latest ["e1", "e3"]
        record_session_roster_correction(
            organization=self.org,
            session_id=s2,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e2"],
            actor_user=self.user_e2,
        )

        eff_conflict = derive_effective_roster(
            self.org, self.vehicle_id, d + timedelta(minutes=1), evaluation_time=d
        )
        self.assertEqual(eff_conflict.status, RosterStatus.CONFLICT)

        # Supervisor resolves c1_new in favor of ["e1", "e2"]
        record_session_roster_correction(
            organization=self.org,
            session_id=s2,
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e2"],
            actor_user=self.user_e1,
            resolves_correction=c1_new,
        )

        # Effective roster must be CORRECTED with ["e1", "e2"], NOT CONFLICT from reviving c1_old ["e1", "e99"]!
        eff_resolved = derive_effective_roster(
            self.org, self.vehicle_id, d + timedelta(minutes=1), evaluation_time=d
        )
        self.assertEqual(eff_resolved.status, RosterStatus.CORRECTED)
        self.assertEqual(eff_resolved.members, frozenset(["e1", "e2"]))

    def test_prior_shift_start_change_excluding_supporting_check_reinstates_waiver(self):
        """Refreshing the prior shift from 00:00-06:00 to 01:00-06:00 excludes a 00:30 check and reinstates the waiver (Codex Inline 4355495705)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        t00_30 = datetime(2026, 1, 15, 0, 30, tzinfo=UTC)
        t01 = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        def check_lookup(org, veh, item, start, end, *, obligation_id=None):
            if obligation_id is not None:
                return None
            if start <= t00_30 < end:
                return {"id": "chk-0030", "passed": True, "checked_at": t00_30}
            return None

        with override_settings(CYA_PRIOR_CHECK_LOOKUP=check_lookup):
            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-prior-check-excluded",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # Refresh prior shift to 01:00-06:00 with unchanged crew ("e1", "e2"): 00:30 check is now outside prior shift!
            shift1_narrowed = RosterShift("s1", ("e1", "e2"), t01, d)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1_narrowed, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

        latest = latest_obligation_determination(self.org, "ob-prior-check-excluded")
        self.assertEqual(latest.determination_type, "REINSTATED")
        self.assertIn("no qualifying valid passed check", latest.explanation)

    def test_fleet_refresh_dropping_one_vehicle_supersedes_and_reinstates_removed_vehicle(self):
        """A fleet-wide refresh (vehicle_id=None) returning only vehicle A drops vehicle B's shifts and reinstates B's waiver (Codex Inline 4355495709)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        veh_a = self.vehicle_id
        veh_b = new_id()

        shift_a1 = RosterShift("sa1", ("e1", "e2"), t00, d, vehicle_id=veh_a)
        shift_a2 = RosterShift("sa2", ("e1", "e2"), d, t12, vehicle_id=veh_a)
        shift_b1 = RosterShift("sb1", ("e1", "e2"), t00, d, vehicle_id=veh_b)
        shift_b2 = RosterShift("sb2", ("e1", "e2"), d, t12, vehicle_id=veh_b)

        # 1. Initial fleet-wide refresh with both vehicles A and B
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_a1, shift_a2, shift_b1, shift_b2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=None,
            interval_start=t00,
            interval_end=t12,
        )

        eval_b = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=veh_b,
            item_id=self.item_id,
            due_at=d,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-b", "passed": True},
            evaluation_time=d,
        )
        self.assertTrue(eval_b.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-fleet-veh-b",
            determination_type="WAIVED",
            vehicle_id=veh_b,
            due_at=d,
            rule=self.rule,
            explanation=eval_b.reason,
            evaluated_inputs=eval_b.evaluated_inputs,
        )

        # 2. Subsequent fleet-wide refresh (vehicle_id=None) for [t00, t12) returns ONLY vehicle A!
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_a1, shift_a2],
            fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            vehicle_id=None,
            interval_start=t00,
            interval_end=t12,
            prior_check_lookup=lambda *args, obligation_id=None: (
                None if obligation_id is not None else {"id": "chk-b", "passed": True}
            ),
        )

        # Effective roster for vehicle B at d must be empty (0 members), NOT stale shift_b2!
        eff_b = derive_effective_roster(self.org, veh_b, d, evaluation_time=d)
        self.assertEqual(eff_b.status, RosterStatus.SCHEDULED)
        self.assertEqual(eff_b.members, frozenset())

        # And vehicle B's waiver must have been automatically reinstated!
        latest_b = latest_obligation_determination(self.org, "ob-fleet-veh-b")
        self.assertEqual(latest_b.determination_type, "REINSTATED")

    def test_delayed_refresh_beyond_30_days_fails_closed_as_stale_and_reinstates(self):
        """Under CND-7, an unchanged delayed refresh at server clock D + 31 days preserves an existing waiver, while new determinations fail closed as STALE (HUM-3, CND-3, CND-7)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        def check_lookup(*args, obligation_id=None):
            if obligation_id is not None:
                return None
            return {
                "id": "chk-1",
                "passed": True,
                "checked_at": datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            }

        with override_settings(CYA_PRIOR_CHECK_LOOKUP=check_lookup):
            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-delayed-31d",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # Advance server clock to D + 31 days while ingesting unchanged data fetched at D + 1m
            self._clock_now = d + timedelta(days=31)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=d + timedelta(minutes=1),
                vehicle_id=self.vehicle_id,
            )

            # Existing waiver remains WAIVED under CND-7
            latest = latest_obligation_determination(self.org, "ob-delayed-31d")
            self.assertEqual(latest.determination_type, "WAIVED")

            # A new determination evaluated at D + 31 days still fails closed as STALE under HUM-3 / CND-3
            new_eval = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=self._clock_now,
            )
            self.assertFalse(new_eval.waived)
            self.assertIn("stale", new_eval.reason.lower())

    def test_fractional_microsecond_999999_boundary_is_strictly_half_open(self):
        """A correction ending at D=06:00:00.999999 is strictly half-open [start, D) and does not govern at D (Codex Inline 4355495718)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d_frac = datetime(2026, 1, 15, 6, 0, 0, 999999, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d_frac)
        shift2 = RosterShift("s2", ("e1", "e3"), d_frac, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Night correction ends at d_frac (06:00:00.999999)
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=t00,
            covers_until=d_frac,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )

        # At d_frac, the night correction [t00, d_frac) has ended; scheduled shift2 ("e1", "e3") governs!
        eff_at_d = derive_effective_roster(
            self.org, self.vehicle_id, d_frac, evaluation_time=d_frac
        )
        self.assertEqual(eff_at_d.status, RosterStatus.SCHEDULED)
        self.assertEqual(eff_at_d.members, frozenset(["e1", "e3"]))

        eval_res = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d_frac,
            rule=self.rule,
            prior_check_lookup=lambda *args: {"id": "chk-1", "passed": True},
            evaluation_time=d_frac,
        )
        self.assertFalse(eval_res.waived)
        self.assertIn("e3", eval_res.reason)

    def test_incomplete_waiver_evidence_on_continuous_shift_reinstates_fail_closed(self):
        """A continuous 00:00-12:00 shift with a seeded WAIVED row lacking prior_shift_start fails closed and reinstates on correction (Claude Finding 1)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        continuous = RosterShift("s-cont", ("e1", "e2"), t00, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[continuous],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Seed a service-path WAIVED determination with prior_members but no prior_shift_start
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-incomplete-cont",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Seeded waiver",
            evaluated_inputs={
                "prior_members": ["e1", "e2"],
                "vehicle_id": str(self.vehicle_id),
                "due_at": d.isoformat(),
            },
        )

        # Day-crew correction {e1, e2} triggers re-evaluation, which must fail closed and reinstate
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )

        latest = latest_obligation_determination(self.org, "ob-incomplete-cont")
        self.assertEqual(latest.determination_type, "REINSTATED")
        self.assertIn("incomplete initial evaluation evidence", latest.explanation)

    def test_cnd7_same_roster_confirmation_after_3_hours_preserves_waiver(self):
        """Confirming the same roster more than 3 hours after the prior shift's last fetch keeps an existing waiver WAIVED (CND-7, OQ-51, Claude Bug/risk 1)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-night-pass",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
            shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-cnd7-same-confirm",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # At 09:00 (3.5h after 05:30 fetch, and past 08:00 grace), day crew confirms the unchanged {e1, e2} roster
            self._clock_now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
            record_session_roster_correction(
                organization=self.org,
                session_id=new_id(),
                vehicle_id=self.vehicle_id,
                covers_from=d,
                covers_until=t12,
                members=["e1", "e2"],
                actor_user=self.user_e1,
            )

            latest = latest_obligation_determination(self.org, "ob-cnd7-same-confirm")
            self.assertEqual(latest.determination_type, "WAIVED")
            self.assertEqual(
                ObligationDetermination.objects.filter(
                    organization=self.org,
                    obligation_id="ob-cnd7-same-confirm",
                ).count(),
                1,
            )

    def test_cnd7_delayed_refresh_with_actual_roster_change_still_reinstates(self):
        """A delayed refresh that actually changes the roster in the dependency window reinstates the waiver even when its fetched_at is old (CND-7 Case 3)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-night-pass",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
            shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                grace_at=grace,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-cnd7-late-change",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # At D + 5h (11:00), a delayed refresh (fetched_at = 05:30, > 3h old at 11:00) arrives with a real change ("e1", "e3")
            self._clock_now = datetime(2026, 1, 15, 11, 0, tzinfo=UTC)
            shift2_changed = RosterShift("s2", ("e1", "e3"), d, t12)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2_changed],
                fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

            latest = latest_obligation_determination(self.org, "ob-cnd7-late-change")
            self.assertEqual(latest.determination_type, "REINSTATED")
            self.assertIn("e3", latest.explanation)
            self.assertTrue(latest.evaluated_inputs.get("is_overdue"))
            self.assertEqual(latest.evaluated_inputs.get("overdue_from"), grace.isoformat())

    def test_cnd7_unavailable_refresh_inside_vs_outside_dependency_window_and_replay(self):
        """An unavailable refresh covering [prior_shift_start, D] reinstates (and replays idempotently); an outage outside that window does not (CND-7 Case 4)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        t18 = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-night-pass",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
            shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-cnd7-outage-scope",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_res.reason,
                evaluated_inputs=eval_res.evaluated_inputs,
            )

            # 1. Outage for [12:00, 18:00) is outside dependency window [00:00, 06:00] -> waiver stays WAIVED
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=datetime(2026, 1, 15, 5, 15, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
                interval_start=t12,
                interval_end=t18,
            )
            self.assertEqual(
                latest_obligation_determination(
                    self.org, "ob-cnd7-outage-scope"
                ).determination_type,
                "WAIVED",
            )

            # 2. Outage for [02:00, 04:00) inside the prior shift [00:00, 06:00) -> reinstates!
            t02 = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)
            t04 = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
            outage_fetched = datetime(2026, 1, 15, 5, 30, tzinfo=UTC)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=outage_fetched,
                vehicle_id=self.vehicle_id,
                interval_start=t02,
                interval_end=t04,
            )
            self.assertEqual(
                latest_obligation_determination(
                    self.org, "ob-cnd7-outage-scope"
                ).determination_type,
                "REINSTATED",
            )
            self.assertEqual(
                ObligationDetermination.objects.filter(
                    organization=self.org,
                    obligation_id="ob-cnd7-outage-scope",
                ).count(),
                2,
            )

            # 3. Replaying the identical outage refresh adds no duplicate determination
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=outage_fetched,
                vehicle_id=self.vehicle_id,
                interval_start=t02,
                interval_end=t04,
            )
            self.assertEqual(
                ObligationDetermination.objects.filter(
                    organization=self.org,
                    obligation_id="ob-cnd7-outage-scope",
                ).count(),
                2,
            )

    def test_condition_b_revalidation_across_workers_and_unavailable_collaborator(self):
        """Valid checks, invalidated checks, and unavailable collaborators behave identically across workers without process-local state (Codex 4355728315, Claude Bug/risk 2, Case 5)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-worker-test",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            eval_res = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertTrue(eval_res.waived)

            for ob_id in ("ob-worker-A", "ob-worker-B"):
                record_obligation_determination(
                    organization=self.org,
                    obligation_id=ob_id,
                    determination_type="WAIVED",
                    vehicle_id=self.vehicle_id,
                    due_at=d,
                    rule=self.rule,
                    explanation=eval_res.reason,
                    evaluated_inputs=eval_res.evaluated_inputs,
                )

            # 1. Valid check: unchanged refresh keeps both WAIVED
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 10, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )
            self.assertEqual(
                latest_obligation_determination(self.org, "ob-worker-A").determination_type,
                "WAIVED",
            )

        # 2. Unavailable collaborator (unconfigured CYA_PRIOR_CHECK_LOOKUP=None or unavailable=True):
        # raises PriorCheckLookupUnavailable and rolls back without fabricating WAIVED or REINSTATED
        with override_settings(CYA_PRIOR_CHECK_LOOKUP=None):
            with self.assertRaises(PriorCheckLookupUnavailable):
                ingest_roster_snapshot(
                    organization=self.org,
                    provider_name="humanity",
                    shifts=[shift1, shift2],
                    fetched_at=datetime(2026, 1, 15, 5, 20, tzinfo=UTC),
                    vehicle_id=self.vehicle_id,
                )
        self.assertEqual(
            latest_obligation_determination(self.org, "ob-worker-A").determination_type,
            "WAIVED",
        )

        stub_checks.unavailable = True
        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            with self.assertRaises(PriorCheckLookupUnavailable):
                record_session_roster_correction(
                    organization=self.org,
                    session_id=new_id(),
                    vehicle_id=self.vehicle_id,
                    covers_from=d,
                    covers_until=t12,
                    members=["e1", "e2"],
                    actor_user=self.user_e1,
                )
        stub_checks.unavailable = False
        self.assertEqual(
            latest_obligation_determination(self.org, "ob-worker-A").determination_type,
            "WAIVED",
        )

        # 3. Actual check invalidation: reinstates identically in a fresh worker (via settings or explicit lookup)
        stub_checks.invalidate_check("chk-worker-test")
        fresh_worker_lookup = StubPriorCheckLookup(checks=stub_checks._checks)
        with override_settings(CYA_PRIOR_CHECK_LOOKUP=fresh_worker_lookup):
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )
            direct_eval = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=self.item_id,
                due_at=d,
                rule=self.rule,
                evaluation_time=d,
            )
            self.assertFalse(direct_eval.waived)

        self.assertEqual(
            latest_obligation_determination(self.org, "ob-worker-A").determination_type,
            "REINSTATED",
        )
        self.assertEqual(
            latest_obligation_determination(self.org, "ob-worker-B").determination_type,
            "REINSTATED",
        )

    def test_cnd7_current_obligation_check_evidence_survives_waiver_withdrawal(self):
        """When a waiver is withdrawn after G, a valid recorded check for the current obligation satisfies it (not overdue), while the prior shift's check is never reused (CND-7 Case 6)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        item_satisfied = self.item_id
        item_unsatisfied = new_id()
        rule_unsatisfied = create_conditional_rule(
            organization=self.org,
            rule_type="CARRY_OVER_FROM_PRIOR_SHIFT",
            revision=2,
            typed_inputs={"item_id": item_unsatisfied},
            effective_at=datetime(2026, 1, 2, tzinfo=UTC),
            actor_user=self.user_e1,
        )

        stub_checks = activate_stub_prior_check_lookup()
        # Prior shift [00:00, 06:00) passing checks for both items
        stub_checks.add_check(
            check_id="chk-prior-1",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(item_satisfied),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )
        stub_checks.add_check(
            check_id="chk-prior-2",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(item_unsatisfied),
            checked_at=datetime(2026, 1, 15, 0, 35, tzinfo=UTC),
            passed=True,
        )

        with override_settings(
            CYA_PRIOR_CHECK_LOOKUP="cya.testing.stubs.active_stub_prior_check_lookup"
        ):
            shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
            shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=[shift1, shift2],
                fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
            )

            eval_1 = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=item_satisfied,
                due_at=d,
                grace_at=grace,
                rule=self.rule,
                evaluation_time=d,
            )
            eval_2 = evaluate_carry_over_rule(
                organization=self.org,
                vehicle_id=self.vehicle_id,
                item_id=item_unsatisfied,
                due_at=d,
                grace_at=grace,
                rule=rule_unsatisfied,
                evaluation_time=d,
            )
            self.assertTrue(eval_1.waived)
            self.assertTrue(eval_2.waived)

            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-with-current-check",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=eval_1.reason,
                evaluated_inputs=eval_1.evaluated_inputs,
            )
            record_obligation_determination(
                organization=self.org,
                obligation_id="ob-without-current-check",
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=rule_unsatisfied,
                explanation=eval_2.reason,
                evaluated_inputs=eval_2.evaluated_inputs,
            )

            # Crew performs a qualifying item-level check for item_satisfied during the current shift at 06:15
            stub_checks.add_check(
                check_id="chk-current-shift-1",
                organization_id=str(self.org.pk),
                vehicle_id=str(self.vehicle_id),
                item_id=str(item_satisfied),
                checked_at=datetime(2026, 1, 15, 6, 15, tzinfo=UTC),
                passed=True,
                obligation_id="ob-with-current-check",
            )

            # At 09:00 (after grace G=08:00), a roster outage withdraws both waivers
            self._clock_now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=datetime(2026, 1, 15, 8, 55, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
                interval_start=t00,
                interval_end=t12,
            )

            det_satisfied = latest_obligation_determination(self.org, "ob-with-current-check")
            det_unsatisfied = latest_obligation_determination(self.org, "ob-without-current-check")

            # 1. Waiver withdrawal restores requirement (REINSTATED), but ob-with-current-check remains satisfied and is NOT overdue
            self.assertEqual(det_satisfied.determination_type, "REINSTATED")
            self.assertTrue(det_satisfied.evaluated_inputs.get("obligation_satisfied"))
            self.assertEqual(
                det_satisfied.evaluated_inputs.get("satisfied_by_check_id"),
                "chk-current-shift-1",
            )
            self.assertFalse(det_satisfied.evaluated_inputs.get("is_overdue"))
            self.assertIsNone(det_satisfied.evaluated_inputs.get("overdue_from"))

            # 2. ob-without-current-check does NOT reuse chk-prior-2 from the prior shift and IS overdue from G
            self.assertEqual(det_unsatisfied.determination_type, "REINSTATED")
            self.assertFalse(det_unsatisfied.evaluated_inputs.get("obligation_satisfied"))
            self.assertIsNone(det_unsatisfied.evaluated_inputs.get("satisfied_by_check_id"))
            self.assertTrue(det_unsatisfied.evaluated_inputs.get("is_overdue"))
            self.assertEqual(
                det_unsatisfied.evaluated_inputs.get("overdue_from"), grace.isoformat()
            )

    def test_reinstatement_satisfaction_requires_explicit_link_and_supports_early_and_failed_checks(
        self,
    ):
        """Reinstatement satisfaction requires an explicit obligation link, rejects unlinked or other-obligation checks, does not let later checks hide own checks, and accepts early [E, D) and Failed checks (Codex 4356132069, 4356132078, Claude 1b/1c)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-prior-0030",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        initial_eval = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=d,
        )
        self.assertTrue(initial_eval.waived)

        for ob_id in (
            "ob-unlinked",
            "ob-other-linked",
            "ob-early-pass",
            "ob-failed-check",
            "ob-hidden-by-later",
        ):
            record_obligation_determination(
                organization=self.org,
                obligation_id=ob_id,
                determination_type="WAIVED",
                vehicle_id=self.vehicle_id,
                due_at=d,
                rule=self.rule,
                explanation=initial_eval.reason,
                evaluated_inputs=initial_eval.evaluated_inputs,
            )

        # 1. Unlinked check at 06:15 and unlinked next-shift check at 12:30 (obligation_id=None)
        stub_checks.add_check(
            check_id="chk-unlinked-0615",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 6, 15, tzinfo=UTC),
            passed=True,
            obligation_id=None,
        )
        stub_checks.add_check(
            check_id="chk-unlinked-1230",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 12, 30, tzinfo=UTC),
            passed=True,
            obligation_id=None,
        )
        # 2. Early on-time check at 05:30 in [E, D) linked to ob-early-pass
        stub_checks.add_check(
            check_id="chk-early-0530",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 5, 30, tzinfo=UTC),
            passed=True,
            obligation_id="ob-early-pass",
        )
        # 3. Valid FAILED check at 06:15 linked to ob-failed-check (SPEC §5.4)
        stub_checks.add_check(
            check_id="chk-failed-0615",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 6, 15, tzinfo=UTC),
            passed=False,
            obligation_id="ob-failed-check",
        )
        # 4. Own check at 06:15 for ob-hidden-by-later PLUS a later check at 12:30 for ob-next
        stub_checks.add_check(
            check_id="chk-own-0615",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 6, 15, tzinfo=UTC),
            passed=True,
            obligation_id="ob-hidden-by-later",
        )
        stub_checks.add_check(
            check_id="chk-other-1230",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 12, 30, tzinfo=UTC),
            passed=True,
            obligation_id="ob-next",
        )

        # Outage at 13:00 (after G=08:00 and after 12:30) withdraws all 5 waivers
        self._clock_now = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 12, 55, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        det_unlinked = latest_obligation_determination(self.org, "ob-unlinked")
        self.assertEqual(det_unlinked.determination_type, "REINSTATED")
        self.assertFalse(det_unlinked.evaluated_inputs.get("obligation_satisfied"))
        self.assertIsNone(det_unlinked.evaluated_inputs.get("satisfied_by_check_id"))
        self.assertTrue(det_unlinked.evaluated_inputs.get("is_overdue"))

        det_other = latest_obligation_determination(self.org, "ob-other-linked")
        self.assertEqual(det_other.determination_type, "REINSTATED")
        self.assertFalse(det_other.evaluated_inputs.get("obligation_satisfied"))
        self.assertIsNone(det_other.evaluated_inputs.get("satisfied_by_check_id"))
        self.assertTrue(det_other.evaluated_inputs.get("is_overdue"))

        det_early = latest_obligation_determination(self.org, "ob-early-pass")
        self.assertEqual(det_early.determination_type, "REINSTATED")
        self.assertTrue(det_early.evaluated_inputs.get("obligation_satisfied"))
        self.assertEqual(det_early.evaluated_inputs.get("satisfied_by_check_id"), "chk-early-0530")
        self.assertFalse(det_early.evaluated_inputs.get("is_overdue"))

        det_failed = latest_obligation_determination(self.org, "ob-failed-check")
        self.assertEqual(det_failed.determination_type, "REINSTATED")
        self.assertTrue(det_failed.evaluated_inputs.get("obligation_satisfied"))
        self.assertEqual(
            det_failed.evaluated_inputs.get("satisfied_by_check_id"), "chk-failed-0615"
        )
        self.assertEqual(det_failed.evaluated_inputs.get("inspection_outcome"), "FAILED")
        self.assertFalse(det_failed.evaluated_inputs.get("is_overdue"))

        det_hidden = latest_obligation_determination(self.org, "ob-hidden-by-later")
        self.assertEqual(det_hidden.determination_type, "REINSTATED")
        self.assertTrue(det_hidden.evaluated_inputs.get("obligation_satisfied"))
        self.assertEqual(det_hidden.evaluated_inputs.get("satisfied_by_check_id"), "chk-own-0615")
        self.assertFalse(det_hidden.evaluated_inputs.get("is_overdue"))

    def test_reinstatement_satisfaction_lookup_outage_rolls_back_and_incomplete_evidence_guard_consistent(
        self,
    ):
        """A lookup outage during reinstatement raises PriorCheckLookupUnavailable and rolls back, and check_and_apply_reinstatement enforces the incomplete-evidence guard (Claude 1a, Suggestion 2)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-prior-outage",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        initial_eval = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=d,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-lookup-outage",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=initial_eval.reason,
            evaluated_inputs=initial_eval.evaluated_inputs,
        )
        stub_checks.add_check(
            check_id="chk-cur-0615",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 6, 15, tzinfo=UTC),
            passed=True,
            obligation_id="ob-lookup-outage",
        )

        # Make check lookup unavailable at 09:00 when roster outage triggers reinstatement
        stub_checks.unavailable = True
        self._clock_now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        with self.assertRaises(PriorCheckLookupUnavailable):
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=datetime(2026, 1, 15, 8, 55, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
                interval_start=t00,
                interval_end=t12,
            )

        # Nothing committed: waiver remains WAIVED rather than committing a false overdue REINSTATED row
        self.assertEqual(
            latest_obligation_determination(self.org, "ob-lookup-outage").determination_type,
            "WAIVED",
        )

        # Restore lookup availability: now reinstatement commits with obligation_satisfied=True, is_overdue=False
        stub_checks.unavailable = False
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 8, 56, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )
        det = latest_obligation_determination(self.org, "ob-lookup-outage")
        self.assertEqual(det.determination_type, "REINSTATED")
        self.assertTrue(det.evaluated_inputs.get("obligation_satisfied"))
        self.assertFalse(det.evaluated_inputs.get("is_overdue"))

        # Verify check_and_apply_reinstatement also enforces the incomplete-evidence guard
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-incomplete-direct",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation="Incomplete waiver",
            evaluated_inputs={
                "vehicle_id": str(self.vehicle_id),
                "item_id": str(self.item_id),
                "due_at": d.isoformat(),
                "condition_a_satisfied": True,
                "condition_b_satisfied": True,
            },
        )
        reinstated_direct = check_and_apply_reinstatement(
            organization=self.org,
            obligation_id="ob-incomplete-direct",
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            current_time=datetime(2026, 1, 15, 9, 5, tzinfo=UTC),
        )
        self.assertIsNotNone(reinstated_direct)
        self.assertEqual(reinstated_direct.determination_type, "REINSTATED")
        self.assertIn("incomplete initial evaluation evidence", reinstated_direct.explanation)

    def test_actual_roster_corrections_do_not_bypass_newer_covering_unavailable_refresh(self):
        """When both prior and current shifts have unchanged actual-roster corrections, a newer UNAVAILABLE refresh covering the dependency window still reinstates the waiver (Codex 4356132089, CND-7)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-prior-corr-outage",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        initial_eval = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=d,
        )
        self.assertTrue(initial_eval.waived)
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-corr-then-outage",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=initial_eval.reason,
            evaluated_inputs=initial_eval.evaluated_inputs,
        )

        # Unchanged actual-roster corrections for both [00:00, 06:00) and [06:00, 12:00)
        self._clock_now = datetime(2026, 1, 15, 6, 10, tzinfo=UTC)
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=t00,
            covers_until=d,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )
        self.assertEqual(
            latest_obligation_determination(self.org, "ob-corr-then-outage").determination_type,
            "WAIVED",
        )

        # Newer UNAVAILABLE refresh covering [00:00, 12:00) must reinstate the waiver
        self._clock_now = datetime(2026, 1, 15, 7, 0, tzinfo=UTC)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 6, 55, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )
        det = latest_obligation_determination(self.org, "ob-corr-then-outage")
        self.assertEqual(det.determination_type, "REINSTATED")
        self.assertTrue(det.evaluated_inputs.get("fail_closed"))

    def test_partial_recovery_of_unavailable_dependency_window_remains_unavailable(self):
        """A partial AVAILABLE recovery covering only a subsegment of an UNAVAILABLE dependency window does not clear unavailability until the full window and due instant are covered (Codex 6368725224 Bug/risk 1)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        t01 = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-prior-partial-rec",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        # Record unchanged actual-roster corrections for both [00:00, 06:00) and [06:00, 12:00)
        self._clock_now = datetime(2026, 1, 15, 6, 5, tzinfo=UTC)
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=t00,
            covers_until=d,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )
        record_session_roster_correction(
            organization=self.org,
            session_id=new_id(),
            vehicle_id=self.vehicle_id,
            covers_from=d,
            covers_until=t12,
            members=["e1", "e2"],
            actor_user=self.user_e1,
        )

        # Outage covering [00:00, 12:00) at fetched_at = 06:20
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 6, 20, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        # 1. Partial AVAILABLE recovery covering ONLY [00:00, 01:00) at fetched_at = 06:30
        shift_partial_1 = RosterShift("s-part-1", ("e1", "e2"), t00, t01)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_partial_1],
            fetched_at=datetime(2026, 1, 15, 6, 30, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t01,
        )

        # Both new evaluation and existing-waiver re-evaluation must still fail closed as UNAVAILABLE
        eval_new_1 = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=datetime(2026, 1, 15, 6, 35, tzinfo=UTC),
        )
        self.assertFalse(eval_new_1.waived)
        self.assertEqual(eval_new_1.evaluated_inputs.get("prior_status"), RosterStatus.UNAVAILABLE)

        eval_existing_1 = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=datetime(2026, 1, 15, 6, 35, tzinfo=UTC),
            re_evaluating_existing_waiver=True,
            prior_shift_bounds=(t00, d),
        )
        self.assertFalse(eval_existing_1.waived)
        self.assertEqual(
            eval_existing_1.evaluated_inputs.get("prior_status"), RosterStatus.UNAVAILABLE
        )

        # 2. Recovering [01:00, 06:00) at fetched_at = 06:40 covers [00:00, 06:00), but due_at=06:00 itself is still unrecovered from the [00:00, 12:00) outage
        shift_partial_2 = RosterShift("s-part-2", ("e1", "e2"), t01, d)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift_partial_2],
            fetched_at=datetime(2026, 1, 15, 6, 40, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t01,
            interval_end=d,
        )
        eval_existing_2 = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=datetime(2026, 1, 15, 6, 45, tzinfo=UTC),
            re_evaluating_existing_waiver=True,
            prior_shift_bounds=(t00, d),
        )
        self.assertFalse(eval_existing_2.waived)
        self.assertEqual(
            eval_existing_2.evaluated_inputs.get("prior_status"), RosterStatus.UNAVAILABLE
        )

        # 3. Recovering [06:00, 12:00) at fetched_at = 06:50 completes coverage of [00:00, 06:00] -> evaluation waives
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift2],
            fetched_at=datetime(2026, 1, 15, 6, 50, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=d,
            interval_end=t12,
        )
        eval_recovered = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=datetime(2026, 1, 15, 6, 55, tzinfo=UTC),
            re_evaluating_existing_waiver=True,
            prior_shift_bounds=(t00, d),
        )
        self.assertTrue(eval_recovered.waived)

    def test_early_check_beyond_12_hours_satisfies_reinstated_obligation_and_strict_collaborator_contract(
        self,
    ):
        """A linked qualifying check in a 24-hour early window (e.g. D - 18h) satisfies a reinstated obligation, and a collaborator lacking obligation_id fails closed as PriorCheckLookupUnavailable (Codex 6368725224 Bug/risk 2, Claude 6367373843 Suggestion)."""
        t00 = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
        d = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)
        grace = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)
        t12 = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        early_24h = d - timedelta(hours=24)  # 2026-01-14T06:00:00Z
        early_12h = d - timedelta(hours=12)  # 2026-01-14T18:00:00Z
        check_at_minus_18h = d - timedelta(hours=18)  # 2026-01-14T12:00:00Z

        stub_checks = activate_stub_prior_check_lookup()
        stub_checks.add_check(
            check_id="chk-prior-shift-0030",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=datetime(2026, 1, 15, 0, 30, tzinfo=UTC),
            passed=True,
        )

        shift1 = RosterShift("s1", ("e1", "e2"), t00, d)
        shift2 = RosterShift("s2", ("e1", "e2"), d, t12)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=[shift1, shift2],
            fetched_at=datetime(2026, 1, 15, 5, 0, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
        )

        eval_24h = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            early_at=early_24h,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=d,
        )
        self.assertTrue(eval_24h.waived)
        self.assertEqual(eval_24h.evaluated_inputs.get("early_at"), early_24h.isoformat())

        eval_12h = evaluate_carry_over_rule(
            organization=self.org,
            vehicle_id=self.vehicle_id,
            item_id=self.item_id,
            due_at=d,
            early_at=early_12h,
            grace_at=grace,
            rule=self.rule,
            evaluation_time=d,
        )
        self.assertTrue(eval_12h.waived)

        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-early-24h",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_24h.reason,
            evaluated_inputs=eval_24h.evaluated_inputs,
        )
        record_obligation_determination(
            organization=self.org,
            obligation_id="ob-early-12h-cutoff",
            determination_type="WAIVED",
            vehicle_id=self.vehicle_id,
            due_at=d,
            rule=self.rule,
            explanation=eval_12h.reason,
            evaluated_inputs=eval_12h.evaluated_inputs,
        )

        # Linked check at D - 18h (inside [early_24h, D) for ob-early-24h, but before early_12h for ob-early-12h-cutoff)
        stub_checks.add_check(
            check_id="chk-early-minus-18h",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=check_at_minus_18h,
            passed=True,
            obligation_id="ob-early-24h",
        )
        stub_checks.add_check(
            check_id="chk-before-early-12h",
            organization_id=str(self.org.pk),
            vehicle_id=str(self.vehicle_id),
            item_id=str(self.item_id),
            checked_at=check_at_minus_18h,
            passed=True,
            obligation_id="ob-early-12h-cutoff",
        )

        # 1. Collaborator lacking obligation_id keyword raises PriorCheckLookupUnavailable during reinstatement and rolls back
        def legacy_collab_without_kwarg(org, veh, item, start, end):
            return {"id": "chk-prior-shift-0030", "passed": True, "checked_at": t00}

        self._clock_now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        with self.assertRaises(PriorCheckLookupUnavailable):
            ingest_roster_snapshot(
                organization=self.org,
                provider_name="humanity",
                shifts=None,
                is_available=False,
                fetched_at=datetime(2026, 1, 15, 8, 54, tzinfo=UTC),
                vehicle_id=self.vehicle_id,
                interval_start=t00,
                interval_end=t12,
                prior_check_lookup=legacy_collab_without_kwarg,
            )

        # 2. Outage at 09:00 (after G=08:00) with compliant collaborator withdraws both waivers
        self._clock_now = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
        ingest_roster_snapshot(
            organization=self.org,
            provider_name="humanity",
            shifts=None,
            is_available=False,
            fetched_at=datetime(2026, 1, 15, 8, 55, tzinfo=UTC),
            vehicle_id=self.vehicle_id,
            interval_start=t00,
            interval_end=t12,
        )

        det_24h = latest_obligation_determination(self.org, "ob-early-24h")
        self.assertEqual(det_24h.determination_type, "REINSTATED")
        self.assertTrue(det_24h.evaluated_inputs.get("obligation_satisfied"))
        self.assertEqual(
            det_24h.evaluated_inputs.get("satisfied_by_check_id"), "chk-early-minus-18h"
        )
        self.assertFalse(det_24h.evaluated_inputs.get("is_overdue"))

        det_12h = latest_obligation_determination(self.org, "ob-early-12h-cutoff")
        self.assertEqual(det_12h.determination_type, "REINSTATED")
        self.assertFalse(det_12h.evaluated_inputs.get("obligation_satisfied"))
        self.assertTrue(det_12h.evaluated_inputs.get("is_overdue"))
