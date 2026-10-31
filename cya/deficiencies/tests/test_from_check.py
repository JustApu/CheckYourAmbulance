"""Failed and Unable to Inspect checks create a deficiency or add evidence atomically (issue 80 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/80))."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

from django.db import connection

from cya.checks.commands import CheckError, invalidate_chain
from cya.checks.conflicts import CheckConflict
from cya.checks.models import CheckRecord
from cya.core.models import AuditEvent
from cya.deficiencies.models import (
    Deficiency,
    DeficiencyAffectedItem,
    DeficiencyEvent,
    DeficiencyEvidence,
    DeficiencyProjection,
)
from cya.deficiencies.tests.support import DeficiencyCase


class FromCheckTests(DeficiencyCase):
    def rejected(self, code, status=422, **kwargs):
        """The call is refused: nothing at all is stored and the operation id stays free."""
        before, checks = self.counts(), CheckRecord.objects.count()
        with self.assertRaises(CheckError) as caught:
            self.failed(**kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(caught.exception.status, status)
        self.assertEqual((self.counts(), CheckRecord.objects.count()), (before, checks))
        return caught.exception

    def test_a_failed_check_creates_a_deficiency_atomically_with_its_evidence(self):
        result = self.failed()
        check = CheckRecord.objects.get()
        deficiency = Deficiency.objects.get()
        self.assertEqual(result.body["deficiency_ids"], [deficiency.pk])
        self.assertEqual(len(result.body["event_ids"]), 1)
        self.assertEqual(
            (
                deficiency.vehicle_id,
                deficiency.item_id,
                deficiency.kit_id,
                deficiency.compartment_id,
            ),
            (self.vehicle.pk, self.suction.pk, None, None),
        )
        self.assertEqual(
            (deficiency.severity_version_id, deficiency.summary, deficiency.category_version_id),
            (self.level("HIGH").pk, "Synthetic summary", self.plain.pk),
        )
        self.assertEqual(
            (deficiency.reporter_id, deficiency.operation_id, deficiency.command_id),
            (self.crew.pk, check.operation_id, check.command_id),
        )
        self.assertEqual(deficiency.reported_at, check.recorded_at)
        event = DeficiencyEvent.objects.get()
        self.assertEqual(
            (event.pk, event.sequence, event.event_type, event.from_status, event.to_status),
            (result.body["event_ids"][0], 1, "REPORTED", None, "OPEN"),
        )
        self.assertIsNone(event.predecessor_event_id)
        projection = DeficiencyProjection.objects.get()
        self.assertEqual(
            (projection.status, projection.assignee_id, projection.last_applied_event_id),
            ("OPEN", None, event.pk),
        )
        scope = DeficiencyAffectedItem.objects.get()
        self.assertEqual((scope.item_id, scope.definition_id), (self.suction.pk, check.item_def_id))
        evidence = DeficiencyEvidence.objects.get()
        self.assertEqual(
            (evidence.check_record_id, evidence.deficiency_id), (check.pk, deficiency.pk)
        )
        audit = AuditEvent.objects.get(action="CHECK_RECORD")
        self.assertEqual(audit.new_snapshot["values"]["deficiency_ids"], [deficiency.pk])
        self.assertNotIn("Synthetic summary", str(audit.new_snapshot))

    def test_a_deficiency_stays_separate_from_the_check_that_spawned_it(self):
        self.failed()
        before = CheckRecord.objects.get().outcome
        self.assertEqual(before, "FAILED")
        self.record(outcome="PASSED")  # a later pass changes nothing about the deficiency
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")
        self.assertEqual(Deficiency.objects.count(), 1)

    def test_unable_to_inspect_needs_a_reason_and_creates_a_deficiency(self):
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.record(outcome="UNABLE_TO_INSPECT", deficiency={"create": self.new_fields()})
        self.assertEqual(caught.exception.code, "EXPLANATION_REQUIRED")
        self.assertEqual(self.counts(), before)
        result = self.record(
            outcome="UNABLE_TO_INSPECT",
            explanation="Synthetic: the cabinet was locked.",
            deficiency={"create": self.new_fields()},
        )
        self.assertEqual(len(result.body["deficiency_ids"]), 1)
        self.assertEqual(CheckRecord.objects.get().outcome, "UNABLE_TO_INSPECT")
        self.assertEqual(Deficiency.objects.count(), 1)

    def test_a_quantity_mismatch_derives_failed_and_still_needs_a_deficiency(self):
        self.rejected(
            "DEFICIENCY_REQUIRED", item=self.masks, outcome=None, quantity=2, deficiency=None
        )
        result = self.record(
            item=self.masks,
            outcome=None,
            quantity=2,
            deficiency={"create": self.new_fields()},
        )
        self.assertEqual(result.body["outcome"], "FAILED")
        self.assertEqual(Deficiency.objects.get().item_id, self.masks.pk)

    def test_the_deficiency_selection_is_required_for_failed_and_forbidden_otherwise(self):
        for outcome in ("FAILED", "UNABLE_TO_INSPECT"):
            with self.subTest(outcome):
                before = self.counts()
                with self.assertRaises(CheckError) as caught:
                    self.record(outcome=outcome, explanation="Synthetic reason.")
                self.assertEqual(caught.exception.code, "DEFICIENCY_REQUIRED")
                self.assertEqual(caught.exception.path, "deficiency")
                self.assertEqual(self.counts(), before)
        before = self.counts()
        with self.assertRaises(CheckError) as caught:
            self.record(outcome="PASSED", deficiency={"create": self.new_fields()})
        self.assertEqual(caught.exception.code, "DEFICIENCY_NOT_ALLOWED")
        self.assertEqual(self.counts(), before)

    def test_a_failed_check_adds_evidence_to_an_explicitly_chosen_deficiency(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        second = self.failed(deficiency={"existing_id": deficiency_id})
        self.assertEqual(second.body["deficiency_ids"], [deficiency_id])
        self.assertEqual(second.body["event_ids"], [])
        self.assertEqual(Deficiency.objects.count(), 1)
        self.assertEqual(DeficiencyEvent.objects.count(), 1)
        self.assertEqual(
            sorted(DeficiencyEvidence.objects.values_list("check_record_id", flat=True)),
            sorted([first.body["check_id"], second.body["check_id"]]),
        )

    def test_several_failed_checks_of_different_items_support_one_deficiency(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        self.failed(self.masks, deficiency={"existing_id": deficiency_id}, quantity=2, outcome=None)
        self.assertEqual(Deficiency.objects.count(), 1)
        self.assertEqual(DeficiencyEvidence.objects.filter(deficiency_id=deficiency_id).count(), 2)
        # The scope snapshot is the reported item's; added evidence does not widen it.
        self.assertEqual(DeficiencyAffectedItem.objects.count(), 1)

    def test_the_server_never_picks_a_match_on_its_own(self):
        self.failed()
        # A second failure of the same item without a selection is refused, not auto-attached.
        self.rejected("DEFICIENCY_REQUIRED", deficiency=None)
        # A second create is a second deficiency even though a match exists.
        self.failed()
        self.assertEqual(Deficiency.objects.count(), 2)

    def test_an_existing_deficiency_must_be_known_active_and_on_the_same_vehicle(self):
        first = self.failed()
        deficiency_id = first.body["deficiency_ids"][0]
        self.rejected("NOT_FOUND", 404, deficiency={"existing_id": str(uuid4())})
        other = self.record(
            vehicle=self.vehicles[1], outcome="FAILED", deficiency={"create": self.new_fields()}
        )
        self.rejected(
            "DEFICIENCY_VEHICLE_MISMATCH",
            deficiency={"existing_id": other.body["deficiency_ids"][0]},
        )
        projection = DeficiencyProjection.objects.get(deficiency_id=deficiency_id)
        for status in (
            "CLOSED_VERIFIED",
            "CLOSED_ADMINISTRATOR",
            "CLOSED_UNVERIFIABLE",
            "REPORTED_IN_ERROR",
        ):
            with self.subTest(status):
                # A fixture-only change: lifecycle handlers that move status arrive with M4.
                DeficiencyProjection.objects.filter(pk=projection.pk).update(status=status)
                self.rejected("DEFICIENCY_NOT_ACTIVE", deficiency={"existing_id": deficiency_id})

    def test_malformed_selections_are_refused_before_anything_is_stored(self):
        for label, value in {
            "not an object": "x",
            "empty": {},
            "both forms": {"existing_id": str(uuid4()), "create": self.new_fields()},
            "unknown key": {"other": 1},
            "bad id": {"existing_id": "nope"},
            "missing field": {"create": {"category_id": self.plain.stable_category_id}},
            "extra field": {"create": {**self.new_fields(), "supply_related": True}},
            "blank summary": {"create": self.new_fields(summary="  ")},
            "long summary": {"create": self.new_fields(summary="x" * 201)},
            "number description": {"create": self.new_fields(description=5)},
            "bad category id": {"create": self.new_fields(category_id="nope")},
        }.items():
            with self.subTest(label):
                self.rejected("MALFORMED_REQUEST", 400, deficiency=value)
        self.rejected("INVALID_SEVERITY", deficiency={"create": self.new_fields(severity="SEVERE")})
        self.rejected(
            "UNKNOWN_CATEGORY", deficiency={"create": self.new_fields(category_id=str(uuid4()))}
        )

    def test_a_category_is_resolved_on_its_current_revision_not_client_input(self):
        stable = str(uuid4())
        self.category("Synthetic v1", supply_related=False, revision=1, stable=stable)
        v2 = self.category("Synthetic v2", supply_related=True, revision=2, stable=stable)
        self.category(
            "Synthetic future",
            supply_related=False,
            revision=3,
            stable=stable,
            when=self.now + timedelta(days=1),
        )
        self.failed(create=self.new_fields(category_id=stable))
        self.assertEqual(Deficiency.objects.get().category_version_id, v2.pk)

    def test_replay_does_not_duplicate_the_check_or_the_deficiency(self):
        operation_id = str(uuid4())
        first = self.failed(operation_id=operation_id)
        counts = self.counts()
        again = self.failed(operation_id=operation_id)
        self.assertEqual((again.body, again.replayed), (first.body, True))
        self.assertEqual(self.counts(), counts)
        self.assertEqual(CheckRecord.objects.count(), 1)

    def test_reusing_an_operation_id_for_a_different_deficiency_applies_nothing(self):
        operation_id = str(uuid4())
        self.failed(operation_id=operation_id)
        counts = self.counts()
        different = self.failed(
            operation_id=operation_id, create=self.new_fields(summary="Synthetic other")
        )
        self.assertTrue(different.operation_reused)
        self.assertEqual(self.counts(), counts)

    def test_the_check_and_deficiency_roll_back_together_when_the_deficiency_is_invalid(self):
        # The deficiency half fails after the check half validated: neither survives.
        self.rejected(
            "UNKNOWN_CATEGORY", deficiency={"create": self.new_fields(category_id=str(uuid4()))}
        )
        self.assertEqual(CheckRecord.objects.count(), 0)

    def test_a_failure_while_writing_the_deficiency_rolls_back_the_check(self):
        from unittest import mock

        with (
            mock.patch(
                "cya.deficiencies.service.add_check_evidence", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.failed()
        self.assertEqual((CheckRecord.objects.count(), Deficiency.objects.count()), (0, 0))
        self.assertFalse(
            AuditEvent.objects.filter(action="CHECK_RECORD", outcome="SUCCEEDED").exists()
        )

    def test_a_failed_check_on_a_system_item_records_the_item(self):
        self.failed(self.siren)
        self.assertEqual(Deficiency.objects.get().item_id, self.siren.pk)

    def test_pass_and_fail_race_keeps_both_checks_and_makes_one_deficiency(self):
        """AC-10 data half: two crew members check one item at once, one Passed, one Failed."""
        second = self.make_user("crew-two")

        def run(job):
            try:
                return job()
            except Exception as error:  # reported to the test, which asserts on the type
                return error
            finally:
                connection.close()

        jobs = [
            lambda: self.record(actor=self.crew, outcome="PASSED"),
            lambda: self.failed(actor=second),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [f.result(timeout=120) for f in [pool.submit(run, j) for j in jobs]]
        committed = [o for o in outcomes if hasattr(o, "body")]
        warned = [o for o in outcomes if isinstance(o, CheckConflict)]
        self.assertEqual((len(committed), len(warned)), (1, 1), outcomes)
        # The warned writer proceeds past the newer check; both records are retained (CON-2).
        newest = committed[0].body["check_id"]
        if committed[0].body["outcome"] == "PASSED":
            self.failed(actor=second, acknowledges_check_id=newest)
        else:
            self.record(actor=self.crew, outcome="PASSED", acknowledges_check_id=newest)
        self.assertEqual(
            sorted(CheckRecord.objects.values_list("outcome", flat=True)), ["FAILED", "PASSED"]
        )
        self.assertEqual(Deficiency.objects.count(), 1)
        self.assertEqual(DeficiencyEvidence.objects.count(), 1)

    def test_the_acknowledged_failure_after_a_pass_makes_one_deficiency(self):
        passed = self.record(actor=self.crew, outcome="PASSED")
        second = self.make_user("crew-two")
        with self.assertRaises(CheckConflict):
            self.failed(actor=second)
        self.assertEqual(Deficiency.objects.count(), 0)
        self.failed(actor=second, acknowledges_check_id=passed.body["check_id"])
        self.assertEqual(CheckRecord.objects.count(), 2)
        self.assertEqual(Deficiency.objects.count(), 1)

    def test_a_warning_creates_no_deficiency_and_no_receipt(self):
        self.record(actor=self.crew, outcome="PASSED")
        second = self.make_user("crew-two")
        before = self.counts()
        with self.assertRaises(CheckConflict):
            self.failed(actor=second)
        self.assertEqual(self.counts(), before)

    def test_every_role_may_report_through_a_check(self):
        for index, actor in enumerate((self.crew, self.supervisor, self.supply, self.admin)):
            with self.subTest(actor=actor.display_name):
                item = (self.suction, self.siren, self.gauge, self.special)[index]
                kwargs = (
                    {"measurement": {"value": "40", "unit": "psi"}} if item is self.gauge else {}
                )
                self.failed(item, actor=actor, **kwargs)
        self.assertEqual(Deficiency.objects.count(), 4)

    def test_invalidating_the_failed_chain_leaves_the_deficiency_open_with_its_evidence(self):
        """INV-3 and DEF-3: invalidation never closes or cancels a deficiency."""
        result = self.failed()
        before = self.counts()
        invalidated = invalidate_chain(
            check_id=result.body["check_id"],
            explanation="Synthetic: recorded on the wrong vehicle.",
            **self.context(self.admin),
        )
        self.assertEqual(invalidated.body["reopened_deficiency_ids"], [])
        counts = self.counts()
        self.assertEqual(counts["DeficiencyEvent"], before["DeficiencyEvent"])
        self.assertEqual(counts["DeficiencyEvidence"], before["DeficiencyEvidence"])
        self.assertEqual(DeficiencyProjection.objects.get().status, "OPEN")
        self.assertEqual(DeficiencyEvidence.objects.get().check_record_id, result.body["check_id"])
