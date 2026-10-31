"""Acceptance tests for record-check obligation linking (issue 74 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/74))."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.db.models.query import QuerySet
from django.test import override_settings

from cya.accounts.fields import new_id
from cya.checks.commands import CheckError, correct_check, invalidate_chain, record_check
from cya.checks.models import (
    CheckEvent,
    CheckObligationLink,
    CheckRecord,
    DeterminationCheck,
    ItemLock,
)
from cya.conditions import check_effects
from cya.conditions.models import ObligationDetermination
from cya.conditions.uti import (
    UnableCompletionError,
    UnableOperationReused,
    complete_unable_obligation,
    invalidate_supervisor_completion,
)
from cya.core.audit_commands import CommandDenied
from cya.core.models import CommandReceipt
from cya.deficiencies.models import (
    DeficiencyCategory,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyProjection,
)
from cya.deficiencies.severity import ensure_default_severities
from cya.kits.tests.support import KitCase
from cya.obligations.adhoc import request_inspection
from cya.obligations.commands import configure_shared_satisfaction_rule
from cya.obligations.models import Obligation
from cya.obligations.projections import get_vehicle_obligations_as_of
from cya.obligations.state import State
from cya.obligations.tests.support import ObligationCase
from cya.testing.role_http import RoleHttpHarness, real_collaborator_settings


class ObligationLinkCleanupMixin:
    def tearDown(self):
        # Generic Django flush uses DELETE, which the new evidence guards reject.
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 0")
            try:
                for table in (
                    "check_obligation_link",
                    "determination_check",
                    "deficiency_link",
                    "deficiency_evidence",
                    "deficiency_event",
                    "deficiency_affected_item",
                    "shared_satisfaction_rule_member",
                    "shared_satisfaction_rule",
                    "item_lock",
                ):
                    cursor.execute(f"TRUNCATE TABLE `{table}`")
            finally:
                cursor.execute("SET SESSION FOREIGN_KEY_CHECKS = 1")
        super().tearDown()


class CheckObligationLinkTests(ObligationLinkCleanupMixin, ObligationCase):
    def setUp(self):
        super().setUp()
        effective_at = self.clock_start - timedelta(days=1)
        ensure_default_severities(self.org.pk, effective_at)
        self.deficiency_category = DeficiencyCategory.objects.create(
            organization=self.org,
            stable_category_id=new_id(),
            revision=1,
            label="Synthetic inspection restriction",
            supply_related=False,
            effective_at=effective_at,
        )
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.version_id = self.published_version(self.base_items())
        self.bind(reconcile=False)
        self.assertEqual(self.generate().failures, [])

    def record_suction(self, actor=None):
        from cya.checks.commands import record_check

        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **self.context(actor or self.crew),
        )

    def record_item(self, item, *, actor=None, **extra):
        from cya.checks.commands import record_check

        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(item.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **extra,
            **self.context(actor or self.crew),
        )

    def record_unable(self, *, item=None, actor=None):
        return record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str((item or self.suction).pk),
            checklist_version_id=self.version_id,
            outcome="UNABLE_TO_INSPECT",
            purpose="ROUTINE",
            explanation="Synthetic access restriction.",
            deficiency={
                "create": {
                    "category_id": str(self.deficiency_category.stable_category_id),
                    "severity": "HIGH",
                    "summary": "Synthetic access restriction",
                    "description": "Synthetic access restriction blocks inspection.",
                }
            },
            **self.context(actor or self.supervisor),
        )

    def complete_unable(
        self, obligation, check_id, *, actor=None, operation_id=None, explanation=None
    ):
        return complete_unable_obligation(
            organization_id=str(self.org.pk),
            actor_id=str((actor or self.supervisor).pk),
            obligation_id=str(obligation.pk),
            check_id=str(check_id),
            explanation=explanation or "Unable inspection resolved by supervisor review.",
            operation_id=operation_id or self.context()["operation_id"],
            correlation_id=self.context()["correlation_id"],
            request_attempt_id=self.context()["request_attempt_id"],
        )

    def test_unable_completion_is_attributed_replayable_and_flagged_for_self_completion(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        operation_id = self.context()["operation_id"]
        completion = self.complete_unable(
            obligation, check.body["check_id"], operation_id=operation_id
        )
        replay = self.complete_unable(obligation, check.body["check_id"], operation_id=operation_id)
        self.assertEqual(replay.pk, completion.pk)
        link = DeterminationCheck.objects.get(determination=completion)
        self.assertEqual(str(link.check_record_id), check.body["check_id"])
        before = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            completion.recorded_at - timedelta(microseconds=1),
            organization_id=str(self.org.pk),
        )
        self.assertNotEqual(
            next(row for row in before if row.obligation.id == str(obligation.pk)).state,
            State.SATISFIED,
        )
        visible = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), completion.recorded_at, organization_id=str(self.org.pk)
        )
        state = next(row for row in visible if row.obligation.id == str(obligation.pk))
        self.assertEqual(state.outcome, "SUPERVISOR_COMPLETED")
        self.assertEqual(state.display_text, "Completed by supervisor — unable to inspect")
        self.assertTrue(state.self_completed)

    def test_unable_completion_denies_crew_and_rejects_unlinked_check(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        with self.assertRaises(CommandDenied):
            self.complete_unable(obligation, check.body["check_id"], actor=self.crew)
        other = Obligation.objects.filter(item=self.masks).first()
        self.assertIsNotNone(other)
        with self.assertRaisesRegex(ValueError, "INELIGIBLE_CHECK"):
            self.complete_unable(other, check.body["check_id"])
        with self.assertRaisesRegex(ValueError, "INVALID_REQUEST"):
            complete_unable_obligation(
                obligation_id=str(obligation.pk),
                check_id=check.body["check_id"],
                explanation="  ",
                **self.context(self.supervisor),
            )

    def test_unable_completion_does_not_satisfy_an_ad_hoc_order(self):
        order = request_inspection(
            vehicle_id=str(self.vehicle.pk),
            items=[{"item_id": str(self.suction.pk), "kit_instance_id": None}],
            due_at=self.at(hours=5, minutes=30),
            explanation="Synthetic targeted inspection.",
            **self.context(self.supervisor),
        )
        obligation = Obligation.objects.get(pk=order.body["obligation_ids"][0])
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        self.assertEqual(check.body["obligation_links"][0]["obligation_id"], str(obligation.pk))
        with self.assertRaisesRegex(UnableCompletionError, "INELIGIBLE_OBLIGATION"):
            self.complete_unable(obligation, check.body["check_id"])
        self.assertFalse(
            ObligationDetermination.objects.filter(obligation_id=str(obligation.pk)).exists()
        )

    def test_administrator_invalidation_is_historical_and_returns_obligation_to_open(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        completion = self.complete_unable(obligation, check.body["check_id"])
        before_invalidation = completion.recorded_at
        invalidated = invalidate_supervisor_completion(
            determination_id=str(completion.pk),
            explanation="Supervisor review was entered in error.",
            **self.context(self.admin),
        )
        self.assertEqual(invalidated.determination_type, "INVALIDATED")
        before = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), before_invalidation, organization_id=str(self.org.pk)
        )
        after = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), invalidated.recorded_at, organization_id=str(self.org.pk)
        )
        self.assertEqual(
            next(row for row in before if row.obligation.id == str(obligation.pk)).outcome,
            "SUPERVISOR_COMPLETED",
        )
        self.assertNotEqual(
            next(row for row in after if row.obligation.id == str(obligation.pk)).state,
            State.SATISFIED,
        )

    def completed_unable(self, **fields):
        self.clock.set(self.at(hours=5))
        check = self.record_unable(**fields)
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        completion = self.complete_unable(obligation, check.body["check_id"])
        return check, obligation, completion

    def obligation_state(self, obligation, instant):
        rows = get_vehicle_obligations_as_of(
            str(self.vehicle.pk), instant, organization_id=str(self.org.pk)
        )
        return next(row for row in rows if row.obligation.id == str(obligation.pk))

    def deficiency_snapshot(self):
        return (
            list(DeficiencyProjection.objects.order_by("pk").values()),
            DeficiencyEvent.objects.count(),
            DeficiencyEvidence.objects.count(),
        )

    def chain_write_counts(self):
        return tuple(
            model.objects.count()
            for model in (
                CheckRecord,
                CheckEvent,
                CheckObligationLink,
                ObligationDetermination,
                DeterminationCheck,
                CommandReceipt,
            )
        )

    def assert_unable_correction(self, outcome):
        check, obligation, completion = self.completed_unable()
        deficiency = self.deficiency_snapshot()
        context = self.context(self.supervisor)
        args = dict(
            check_id=check.body["check_id"],
            expected_tip_check_id=check.body["check_id"],
            outcome=outcome,
            **context,
        )
        corrected = correct_check(**args)
        child = ObligationDetermination.objects.get(prior_determination=completion)
        self.assertEqual(child.determination_type, "SUPERSEDED")
        self.assertEqual(child.command.operation_id, context["operation_id"])
        self.assertEqual(child.actor_user_id, self.supervisor.pk)
        self.assertEqual(child.evaluated_inputs["check_event_id"], corrected.body["event_id"])
        self.assertEqual(
            self.obligation_state(obligation, completion.recorded_at).outcome,
            "SUPERVISOR_COMPLETED",
        )
        self.assertEqual(self.obligation_state(obligation, child.recorded_at).outcome, outcome)
        before_replay = self.chain_write_counts()
        self.assertEqual(correct_check(**args).body, corrected.body)
        self.assertEqual(self.chain_write_counts(), before_replay)
        self.assertEqual(self.deficiency_snapshot(), deficiency)
        self.assertEqual(
            CheckRecord.objects.get(pk=check.body["check_id"]).outcome, "UNABLE_TO_INSPECT"
        )

    def test_unable_to_pass_supersedes_completion_with_as_of_history_and_replay(self):
        self.assert_unable_correction("PASSED")

    def test_unable_to_fail_supersedes_completion_with_as_of_history_and_replay(self):
        self.assert_unable_correction("FAILED")

    def test_unable_quantity_correction_requires_a_real_observation(self):
        check, _, completion = self.completed_unable(item=self.masks)
        args = dict(
            check_id=check.body["check_id"],
            expected_tip_check_id=check.body["check_id"],
            outcome="FAILED",
            **self.context(self.supervisor),
        )
        before = self.chain_write_counts()
        with self.assertRaises(CheckError) as caught:
            correct_check(**args)
        self.assertEqual(caught.exception.code, "INVALID_OBSERVATION")
        self.assertEqual(self.chain_write_counts(), before)
        corrected = correct_check(**args, quantity=0)
        self.assertEqual(corrected.body["outcome"], "FAILED")
        self.assertEqual(
            ObligationDetermination.objects.get(prior_determination=completion).determination_type,
            "SUPERSEDED",
        )

    def test_chain_invalidation_voids_completion_atomically_and_replays_without_writes(self):
        check, obligation, completion = self.completed_unable()
        deficiency = self.deficiency_snapshot()
        args = dict(
            check_id=check.body["check_id"],
            explanation="Synthetic invalid evidence.",
            **self.context(self.admin),
        )
        result = invalidate_chain(**args)
        self.assertEqual(result.body["voided_determination_ids"], [str(completion.pk)])
        child = ObligationDetermination.objects.get(prior_determination=completion)
        self.assertEqual(child.determination_type, "INVALIDATED")
        self.assertEqual(child.command.operation_id, args["operation_id"])
        self.assertEqual(child.actor_user_id, self.admin.pk)
        self.assertEqual(
            self.obligation_state(obligation, completion.recorded_at).outcome,
            "SUPERVISOR_COMPLETED",
        )
        self.assertNotEqual(
            self.obligation_state(obligation, child.recorded_at).state, State.SATISFIED
        )
        counts = self.chain_write_counts()
        self.assertEqual(invalidate_chain(**args).body, result.body)
        self.assertEqual(self.chain_write_counts(), counts)
        self.assertEqual(self.deficiency_snapshot(), deficiency)

    def test_conditional_satisfaction_lookup_preserves_completion_before_chain_event(self):
        from cya.conditions.evaluator import _find_qualifying_current_obligation_check

        check, obligation, completion = self.completed_unable()
        result = invalidate_chain(
            check_id=check.body["check_id"],
            explanation="Synthetic invalid evidence.",
            **self.context(self.admin),
        )
        event = CheckEvent.objects.get(pk=result.body["invalidation_event_id"])
        args = dict(
            obligation_id=str(obligation.pk),
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            due_at=obligation.due_at,
            grace_at=obligation.grace_at,
            early_at=obligation.early_at,
            prior_check_id=None,
            prior_check_lookup=lambda *args, **kwargs: None,
        )
        before = _find_qualifying_current_obligation_check(
            self.org, evaluation_time=completion.recorded_at, **args
        )
        after = _find_qualifying_current_obligation_check(
            self.org, evaluation_time=event.recorded_at, **args
        )
        self.assertEqual(before["id"], str(completion.pk))
        self.assertIsNone(after)

    def test_invalidating_a_corrected_unable_chain_does_not_revive_its_completion(self):
        check, obligation, completion = self.completed_unable()
        correction = correct_check(
            check_id=check.body["check_id"],
            expected_tip_check_id=check.body["check_id"],
            outcome="PASSED",
            **self.context(self.supervisor),
        )
        superseded = ObligationDetermination.objects.get(prior_determination=completion)
        result = invalidate_chain(
            check_id=correction.body["check_id"],
            explanation="Synthetic invalid chain.",
            **self.context(self.admin),
        )
        self.assertEqual(result.body["voided_determination_ids"], [])
        instant = CheckEvent.objects.get(pk=result.body["invalidation_event_id"]).recorded_at
        self.assertNotEqual(self.obligation_state(obligation, instant).state, State.SATISFIED)
        # Fresh evidence can be completed again after the old chain has been excluded.
        fresh = self.record_unable()
        new_completion = self.complete_unable(obligation, fresh.body["check_id"])
        self.assertEqual(new_completion.prior_determination_id, superseded.pk)
        self.assertEqual(
            self.obligation_state(obligation, new_completion.recorded_at).outcome,
            "SUPERVISOR_COMPLETED",
        )

    def test_completion_effect_failure_rolls_back_the_check_chain_and_its_receipt(self):
        check, _, _ = self.completed_unable()
        before = self.chain_write_counts()
        append = check_effects.append_check_effects

        def fail_after_append(*args, **kwargs):
            append(*args, **kwargs)
            raise ValueError("Synthetic effect failure")

        jobs = (
            lambda: correct_check(
                check_id=check.body["check_id"],
                expected_tip_check_id=check.body["check_id"],
                outcome="PASSED",
                **self.context(self.supervisor),
            ),
            lambda: invalidate_chain(
                check_id=check.body["check_id"],
                explanation="Synthetic reason.",
                **self.context(self.admin),
            ),
        )
        for job in jobs:
            with mock.patch.object(
                check_effects, "append_check_effects", side_effect=fail_after_append
            ):
                with self.assertRaisesRegex(ValueError, "Synthetic effect failure"):
                    job()
            self.assertEqual(self.chain_write_counts(), before)

    def test_database_refuses_a_completion_effect_without_its_causal_check_event(self):
        check, _, _ = self.completed_unable()
        before = self.chain_write_counts()
        append = check_effects.append_check_effects

        def forged_event(*args, **kwargs):
            kwargs["event_id"] = str(uuid4())
            append(*args, **kwargs)

        with mock.patch.object(check_effects, "append_check_effects", side_effect=forged_event):
            with self.assertRaises(DatabaseError):
                invalidate_chain(
                    check_id=check.body["check_id"],
                    explanation="Synthetic reason.",
                    **self.context(self.admin),
                )
        self.assertEqual(self.chain_write_counts(), before)

    def test_oversized_completion_effect_scope_fails_before_business_writes(self):
        check, _, _ = self.completed_unable()
        before = self.chain_write_counts()
        with mock.patch.object(check_effects, "MAX_COMPLETIONS", 0):
            with self.assertRaises(CheckError) as caught:
                invalidate_chain(
                    check_id=check.body["check_id"],
                    explanation="Synthetic reason.",
                    **self.context(self.admin),
                )
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (503, "CONFIGURATION_UNAVAILABLE")
        )
        self.assertEqual(self.chain_write_counts(), before)

    def test_invalidation_voids_every_shared_obligation_completion(self):
        obligations = self.prepare_overlapping_obligations()
        configure_shared_satisfaction_rule(
            members=[[str(row.requirement_version_id) for row in obligations]],
            **self.context(self.admin),
        )
        check = self.record_unable()
        self.assertEqual(len(check.body["obligation_links"]), 2)
        completions = [self.complete_unable(row, check.body["check_id"]) for row in obligations]
        result = invalidate_chain(
            check_id=check.body["check_id"],
            explanation="Synthetic invalid shared evidence.",
            **self.context(self.admin),
        )
        self.assertEqual(
            set(result.body["voided_determination_ids"]), {str(row.pk) for row in completions}
        )
        for row in completions:
            self.assertEqual(
                ObligationDetermination.objects.get(prior_determination=row).determination_type,
                "INVALIDATED",
            )

    def test_concurrent_completion_and_invalidation_cannot_leave_a_live_completion(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        start = threading.Barrier(2)

        def run(job):
            try:
                start.wait(timeout=20)
                return job()
            except Exception as error:  # asserted below
                return error
            finally:
                connection.close()

        jobs = (
            lambda: self.complete_unable(obligation, check.body["check_id"]),
            lambda: invalidate_chain(
                check_id=check.body["check_id"],
                explanation="Synthetic invalid evidence.",
                **self.context(self.admin),
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            completion, invalidation = [future.result(timeout=120) for future in futures]
        self.assertTrue(hasattr(invalidation, "body"), invalidation)
        if isinstance(completion, ObligationDetermination):
            self.assertEqual(invalidation.body["voided_determination_ids"], [str(completion.pk)])
            self.assertEqual(
                ObligationDetermination.objects.get(
                    prior_determination=completion
                ).determination_type,
                "INVALIDATED",
            )
        else:
            self.assertIsInstance(completion, UnableCompletionError)
            self.assertEqual(completion.code, "INELIGIBLE_CHECK")
        instant = CheckEvent.objects.get(pk=invalidation.body["invalidation_event_id"]).recorded_at
        self.assertNotEqual(self.obligation_state(obligation, instant).state, State.SATISFIED)

    def test_completion_rejects_reused_operation_id_with_changed_explanation(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        operation_id = self.context(self.supervisor)["operation_id"]
        completion = self.complete_unable(
            obligation,
            check.body["check_id"],
            actor=self.supervisor,
            operation_id=operation_id,
            explanation="Original supervisor explanation.",
        )

        with self.assertRaises(UnableOperationReused):
            self.complete_unable(
                obligation,
                check.body["check_id"],
                actor=self.supervisor,
                operation_id=operation_id,
                explanation="Changed supervisor explanation.",
            )

        self.assertEqual(ObligationDetermination.objects.count(), 1)
        self.assertEqual(ObligationDetermination.objects.get().explanation, completion.explanation)

    def test_invalidation_rejects_reused_operation_id_with_changed_explanation(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation = Obligation.objects.get(pk=check.body["obligation_links"][0]["obligation_id"])
        completion = self.complete_unable(obligation, check.body["check_id"])
        context = self.context(self.admin)
        invalidation = invalidate_supervisor_completion(
            determination_id=str(completion.pk),
            explanation="Original invalidation explanation.",
            **context,
        )

        with self.assertRaises(UnableOperationReused):
            invalidate_supervisor_completion(
                determination_id=str(completion.pk),
                explanation="Changed invalidation explanation.",
                **{**context, "correlation_id": str(uuid4()), "request_attempt_id": str(uuid4())},
            )

        self.assertEqual(ObligationDetermination.objects.count(), 2)
        self.assertEqual(
            ObligationDetermination.objects.get(pk=invalidation.pk).explanation,
            "Original invalidation explanation.",
        )

    def test_http_completion_and_invalidation_reject_changed_intent_replays(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        obligation_id = check.body["obligation_links"][0]["obligation_id"]

        with override_settings(**real_collaborator_settings()):
            harness = RoleHttpHarness(organization=self.org)
            supervisor_client, _ = harness.sign_in("supervisor")
            supervisor_token = supervisor_client.cookies["csrftoken"].value
            completion_body = {
                "operation_id": str(uuid4()),
                "check_id": check.body["check_id"],
                "explanation": "Original HTTP completion explanation.",
            }
            completion_path = f"/api/v1/obligations/{obligation_id}/unable-completions"
            first_completion = supervisor_client.post(
                completion_path,
                data=json.dumps(completion_body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=supervisor_token,
            )
            self.assertEqual(first_completion.status_code, 201, first_completion.content)
            reused_completion = supervisor_client.post(
                completion_path,
                data=json.dumps(
                    {**completion_body, "explanation": "Changed HTTP completion explanation."}
                ),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=supervisor_token,
            )
            self.assertEqual(
                (reused_completion.status_code, reused_completion.json()["code"]),
                (409, "OPERATION_REUSED"),
            )
            self.assertEqual(
                reused_completion.json()["field_errors"],
                [{"path": "operation_id", "code": "REUSED"}],
            )

            admin_client, _ = harness.sign_in("admin")
            admin_token = admin_client.cookies["csrftoken"].value
            other_actor_replay = admin_client.post(
                completion_path,
                data=json.dumps(completion_body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=admin_token,
            )
            self.assertEqual(
                (other_actor_replay.status_code, other_actor_replay.json()["code"]),
                (409, "OPERATION_OWNED_BY_ANOTHER_ACTOR"),
            )
            determination_id = first_completion.json()["result"]["determination_id"]
            invalidation_path = (
                f"/api/v1/obligation-determinations/{determination_id}/invalidations"
            )
            invalidation_body = {
                "operation_id": str(uuid4()),
                "explanation": "Original HTTP invalidation explanation.",
            }
            first_invalidation = admin_client.post(
                invalidation_path,
                data=json.dumps(invalidation_body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=admin_token,
            )
            self.assertEqual(first_invalidation.status_code, 201, first_invalidation.content)
            reused_invalidation = admin_client.post(
                invalidation_path,
                data=json.dumps(
                    {**invalidation_body, "explanation": "Changed HTTP invalidation explanation."}
                ),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=admin_token,
            )
            self.assertEqual(
                (reused_invalidation.status_code, reused_invalidation.json()["code"]),
                (409, "OPERATION_REUSED"),
            )
            self.assertEqual(
                reused_invalidation.json()["field_errors"],
                [{"path": "operation_id", "code": "REUSED"}],
            )

        self.assertEqual(ObligationDetermination.objects.count(), 2)

    def test_http_unable_refusals_have_statuses_and_leave_no_business_write(self):
        self.clock.set(self.at(hours=5))
        check = self.record_unable()
        check_id = check.body["check_id"]
        obligation_id = check.body["obligation_links"][0]["obligation_id"]

        with override_settings(**real_collaborator_settings()):
            harness = RoleHttpHarness(organization=self.org)
            client, _ = harness.sign_in("supervisor")
            token = client.cookies["csrftoken"].value

            def post(client, token, path, body):
                before = (
                    ObligationDetermination.objects.count(),
                    DeterminationCheck.objects.count(),
                    CommandReceipt.objects.count(),
                )
                response = client.post(
                    path,
                    data=json.dumps(body),
                    content_type="application/json",
                    HTTP_X_CSRFTOKEN=token,
                )
                after = (
                    ObligationDetermination.objects.count(),
                    DeterminationCheck.objects.count(),
                    CommandReceipt.objects.count(),
                )
                return response, before, after

            def refused(client, token, path, body, status, code):
                response, before, after = post(client, token, path, body)
                self.assertEqual((response.status_code, response.json()["code"]), (status, code))
                self.assertEqual(response.json()["status"], "REJECTED")
                self.assertEqual(before, after)
                self.assertNotIn("Synthetic refusal note", response.content.decode())

            path = f"/api/v1/obligations/{obligation_id}/unable-completions"

            def body(**changes):
                return {
                    "operation_id": str(uuid4()),
                    "check_id": check_id,
                    "explanation": "Synthetic refusal note",
                    **changes,
                }

            refused(client, token, path, body(explanation=" "), 400, "INVALID_REQUEST")
            refused(client, token, path, body(explanation="x" * 4001), 400, "INVALID_REQUEST")
            refused(
                client,
                token,
                "/api/v1/obligations/bad/unable-completions",
                body(),
                400,
                "INVALID_REQUEST",
            )
            refused(client, token, path, body(check_id="bad"), 400, "INVALID_REQUEST")
            refused(
                client,
                token,
                f"/api/v1/obligations/{uuid4()}/unable-completions",
                body(),
                404,
                "NOT_FOUND",
            )
            refused(client, token, path, body(check_id=str(uuid4())), 404, "NOT_FOUND")
            other_obligation = Obligation.objects.filter(item=self.masks).first()
            self.assertIsNotNone(other_obligation)
            refused(
                client,
                token,
                f"/api/v1/obligations/{other_obligation.pk}/unable-completions",
                body(),
                422,
                "INELIGIBLE_CHECK",
            )
            with mock.patch(
                "cya.conditions.uti._item_lock",
                side_effect=UnableCompletionError("ITEM_LOCK_UNAVAILABLE"),
            ):
                refused(client, token, path, body(), 503, "ITEM_LOCK_UNAVAILABLE")

            committed, _, _ = post(client, token, path, body())
            self.assertEqual(committed.status_code, 201, committed.content)
            refused(client, token, path, body(), 409, "OBLIGATION_ALREADY_DETERMINED")

            completion_id = committed.json()["result"]["determination_id"]
            invalidation_path = f"/api/v1/obligation-determinations/{completion_id}/invalidations"
            invalidation_body = {
                "operation_id": str(uuid4()),
                "explanation": "Synthetic refusal note",
            }
            admin_client, _ = harness.sign_in("admin")
            admin_token = admin_client.cookies["csrftoken"].value
            refused(
                admin_client,
                admin_token,
                f"/api/v1/obligation-determinations/{uuid4()}/invalidations",
                invalidation_body,
                404,
                "NOT_FOUND",
            )
            first = admin_client.post(
                invalidation_path,
                data=json.dumps(invalidation_body),
                content_type="application/json",
                HTTP_X_CSRFTOKEN=admin_token,
            )
            self.assertEqual(first.status_code, 201, first.content)
            refused(
                admin_client,
                admin_token,
                invalidation_path,
                {"operation_id": str(uuid4()), "explanation": "Synthetic refusal note"},
                409,
                "NOT_CURRENT_DETERMINATION",
            )
            invalidation_id = first.json()["result"]["determination_id"]
            refused(
                admin_client,
                admin_token,
                f"/api/v1/obligation-determinations/{invalidation_id}/invalidations",
                {"operation_id": str(uuid4()), "explanation": "Synthetic refusal note"},
                409,
                "NOT_CURRENT_DETERMINATION",
            )

    def test_two_crew_members_link_distinct_item_checks_without_a_session(self):
        self.clock.set(self.at(hours=5, minutes=15))
        second_crew = self.make_user("crew-two")
        first = self.record_item(self.suction, actor=self.crew)
        second = self.record_item(self.masks, actor=second_crew, quantity=6)
        self.assertEqual(len(first.body["obligation_links"]), 1)
        self.assertEqual(len(second.body["obligation_links"]), 1)
        self.assertEqual(CheckObligationLink.objects.count(), 2)
        self.assertEqual(
            set(CheckObligationLink.objects.values_list("actor_user_id", flat=True)),
            {self.crew.pk, second_crew.pk},
        )
        self.assertFalse(CheckRecord.objects.filter(session__isnull=False).exists())

    def test_on_time_link_is_written_with_check_and_visible_as_of_T(self):
        self.clock.set(self.at(hours=5, minutes=15))
        response = self.record_suction()
        links = response.body["obligation_links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["timing"], "ON_TIME")
        self.assertIsNone(links[0]["shared_rule_id"])
        row = CheckObligationLink.objects.get(check_record_id=response.body["check_id"])
        self.assertEqual(str(row.obligation_id), links[0]["obligation_id"])
        before_T = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            self.at(hours=5, minutes=14),
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        before = next(s for s in before_T if s.obligation.item_id == str(self.suction.pk))
        self.assertNotEqual(before.state, State.SATISFIED)
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            row.recorded_at,
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        state = next(s for s in states if s.obligation.item_id == str(self.suction.pk))
        self.assertEqual(state.state, State.SATISFIED)

    def test_correction_recomputes_links_at_the_root_instant_and_is_visible_as_of_correction(self):
        self.clock.set(self.at(hours=5, minutes=15))
        original = self.record_suction().body["check_id"]
        self.clock.set(self.at(hours=9))
        result = correct_check(
            check_id=original,
            expected_tip_check_id=original,
            outcome="PASSED",
            **self.context(self.crew),
        )
        replacement = CheckRecord.objects.get(pk=result.body["check_id"])
        link = CheckObligationLink.objects.get(check_record=replacement)
        self.assertEqual(link.recorded_at, replacement.recorded_at)
        self.assertEqual(
            str(link.obligation_id), result.body["obligation_links"][0]["obligation_id"]
        )
        before = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            self.at(hours=8, minutes=59),
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        after = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            self.at(hours=9, minutes=1),
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        self.assertEqual(
            next(row.state for row in before if row.obligation.item_id == str(self.suction.pk)),
            State.SATISFIED,
        )
        self.assertEqual(
            next(row.state for row in after if row.obligation.item_id == str(self.suction.pk)),
            State.SATISFIED,
        )

    def test_too_early_check_is_retained_without_an_obligation_link(self):
        self.clock.set(self.at(hours=4, minutes=59))
        response = self.record_suction()
        self.assertEqual(response.body["obligation_links"], [])
        self.assertFalse(
            CheckObligationLink.objects.filter(check_record_id=response.body["check_id"]).exists()
        )

    def test_late_link_is_used_only_after_the_grace_window(self):
        self.clock.set(self.at(hours=7, minutes=1))
        response = self.record_suction()
        self.assertEqual(response.body["obligation_links"][0]["timing"], "LATE")

    def test_a_later_repeated_check_is_retained_but_does_not_replace_first_satisfaction(self):
        self.clock.set(self.at(hours=5, minutes=15))
        first = self.record_suction()
        self.clock.set(self.at(hours=5, minutes=20))
        second = self.record_suction()
        self.assertNotEqual(first.body["check_id"], second.body["check_id"])
        self.assertEqual(CheckRecord.objects.count(), 2)
        self.assertEqual(len(first.body["obligation_links"]), 1)
        self.assertEqual(second.body["obligation_links"], [])
        self.assertEqual(CheckObligationLink.objects.count(), 1)
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            self.at(hours=5, minutes=21),
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        state = next(s for s in states if s.obligation.item_id == str(self.suction.pk))
        self.assertEqual(state.state, State.SATISFIED)

    def prepare_overlapping_obligations(self):
        # The ordinary case has one eligible obligation; add a second slot whose window overlaps.
        self.publish_schedule("test-0700", wall_time=time(7), recurrence="DAILY")
        self.bind(slot="test-0700", reconcile=False)
        self.assertEqual(self.generate().failures, [])
        self.clock.set(self.at(hours=6, minutes=30))
        return list(
            Obligation.objects.filter(
                vehicle_id=self.vehicle.pk,
                item_id=self.suction.pk,
                due_at__in=(self.at(hours=6), self.at(hours=7)),
            ).order_by("due_at")
        )

    def test_overlapping_obligations_default_to_the_earliest_only(self):
        candidates = self.prepare_overlapping_obligations()
        self.assertEqual(len(candidates), 2)
        response = self.record_suction()
        self.assertEqual(len(response.body["obligation_links"]), 1)

    def test_a_waiting_check_gets_its_time_after_the_check_ahead_of_it(self):
        candidates = self.prepare_overlapping_obligations()
        self.assertEqual(len(candidates), 2)
        second_crew = self.make_user("crew-two")

        waiter_at_item_lock = threading.Event()
        release_waiter = threading.Event()
        worker = threading.local()
        original_first = QuerySet.first

        def controlled_first(queryset):
            if queryset.model is ItemLock and getattr(worker, "name", None) == "waiter":
                waiter_at_item_lock.set()
                if not release_waiter.wait(timeout=20):
                    raise TimeoutError("test did not release the waiting check")
            return original_first(queryset)

        def submit(name):
            worker.name = name
            try:
                return self.record_suction(self.crew if name == "waiter" else second_crew)
            finally:
                connection.close()

        with (
            # This test is about the evidence instant and linking order, not the duplicate
            # warning (CON-3, covered in test_conflicts): keep two different users from warning.
            mock.patch("cya.checks.commands.conflicting_check", return_value=None),
            mock.patch.object(QuerySet, "first", controlled_first),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            waiter = pool.submit(submit, "waiter")
            self.assertTrue(waiter_at_item_lock.wait(timeout=20))
            winner = pool.submit(submit, "winner")
            try:
                winner_result = winner.result(timeout=30)
            finally:
                release_waiter.set()
            waiter_result = waiter.result(timeout=30)

        winner_check = CheckRecord.objects.get(pk=winner_result.body["check_id"])
        waiter_check = CheckRecord.objects.get(pk=waiter_result.body["check_id"])
        self.assertLess(winner_check.recorded_at, waiter_check.recorded_at)
        winner_obligation = Obligation.objects.get(
            pk=winner_result.body["obligation_links"][0]["obligation_id"]
        )
        waiter_obligation = Obligation.objects.get(
            pk=waiter_result.body["obligation_links"][0]["obligation_id"]
        )
        self.assertEqual(winner_obligation.pk, candidates[0].pk)
        self.assertEqual(waiter_obligation.pk, candidates[1].pk)

    def test_database_rejects_an_unruled_additional_obligation_link(self):
        candidates = self.prepare_overlapping_obligations()
        self.assertEqual(len(candidates), 2)
        response = self.record_suction()
        check = CheckRecord.objects.get(pk=response.body["check_id"])
        with self.assertRaises(DatabaseError), transaction.atomic():
            CheckObligationLink.objects.create(
                organization=self.org,
                check_record=check,
                obligation=candidates[1],
                ordinal=1,
                shared_rule=None,
                command=check.command,
                recorded_at=check.recorded_at,
                actor_user=check.actor_user,
                actor_roles_at_time=check.actor_roles_at_time,
            )

    def test_overlapping_obligations_share_only_when_the_current_rule_names_them(self):
        candidates = self.prepare_overlapping_obligations()
        self.assertEqual(len(candidates), 2)
        configure_shared_satisfaction_rule(
            members=[
                [
                    str(candidates[0].requirement_version_id),
                    str(candidates[1].requirement_version_id),
                ]
            ],
            **self.context(self.admin),
        )
        shared = self.record_suction()
        self.assertEqual(len(shared.body["obligation_links"]), 2)
        self.assertIsNotNone(shared.body["obligation_links"][1]["shared_rule_id"])


class KitObligationLinkTests(ObligationLinkCleanupMixin, KitCase):
    def setUp(self):
        super().setUp()
        self.publish_schedule("test-0600", recurrence="DAILY")
        self.reference_kit = self.add_kit("Reference").body["kit_id"]
        self.items = [
            *self.base_items(),
            self.spec(self.kit_item, 80, kit_id=self.reference_kit),
        ]
        self.version_id = self.published_version(self.items)
        self.bind(reconcile=False)
        self.second_kit = self.add_kit("Second").body["kit_id"]
        self.assertEqual(self.generate().failures, [])

    def test_check_links_only_the_named_kit_instance_obligation(self):
        from cya.checks.commands import record_check

        self.clock.set(self.at(hours=5, minutes=15))
        response = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.kit_item.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            kit_id=self.reference_kit,
            **self.context(self.crew),
        )
        link = CheckObligationLink.objects.get(check_record_id=response.body["check_id"])
        self.assertEqual(str(link.obligation.kit_instance_id), self.reference_kit)
        states = get_vehicle_obligations_as_of(
            str(self.vehicle.pk),
            link.recorded_at,
            organization_id=str(self.org.pk),
            start=self.at(hours=6),
            end=self.at(hours=6),
        )
        kit_states = [row for row in states if row.obligation.item_id == str(self.kit_item.pk)]
        self.assertEqual(len(kit_states), 2)
        by_kit = {row.obligation.kit_instance_id: row.state for row in kit_states}
        self.assertEqual(by_kit[self.reference_kit], State.SATISFIED)
        self.assertNotEqual(by_kit[self.second_kit], State.SATISFIED)


class FallBackCheckLinkTests(ObligationCase):
    clock_start = datetime(2026, 10, 31, 12, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        from cya.accounts.models import Organization

        Organization.objects.filter(pk=self.org.pk).update(station_zone="America/New_York")
        self.org.refresh_from_db()
        self.version_id = self.published_version(self.base_items())
        self.publish_schedule("dst-slot", wall_time=time(1, 30), recurrence="DAILY")
        self.bind(slot="dst-slot", reconcile=False)
        self.assertEqual(self.generate(horizon=72).failures, [])

    def test_check_links_to_the_first_absolute_instant_of_fall_back_hour(self):
        from cya.checks.commands import record_check

        self.clock.set(datetime(2026, 11, 1, 5, 45, tzinfo=UTC))
        response = record_check(
            vehicle_id=str(self.vehicle.pk),
            item_id=str(self.suction.pk),
            checklist_version_id=self.version_id,
            outcome="PASSED",
            purpose="ROUTINE",
            **self.context(self.crew),
        )
        link = CheckObligationLink.objects.get(check_record_id=response.body["check_id"])
        self.assertEqual(link.obligation.due_at, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(response.body["obligation_links"][0]["timing"], "ON_TIME")
